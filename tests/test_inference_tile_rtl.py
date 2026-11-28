"""Cycle-accurate M4.4 integration tests for the complete ORCUS inference tile."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orcus.dadda_pipeline import generate_dadda_pipelined
from orcus.inference_tile import (
    TileConfig, mac_accumulator_bounds, signed_bounds,
)

ROOT = Path(__file__).resolve().parents[1]
RTL = ROOT / "orcus" / "rtl" / "orcus_inference_tile.sv"
SCRATCHPAD = ROOT / "orcus" / "rtl" / "orcus_operand_scratchpad.sv"
CONTROLLER = ROOT / "orcus" / "rtl" / "orcus_kloop_controller.sv"


def _testbench(lanes: int) -> str:
    cfg = TileConfig(lanes)
    template = r"""`timescale 1ns/1ps
module tb;
    localparam integer LANES = @LANES@;
    localparam integer WIDTH = LANES * 4;
    localparam integer DEPTH = 16384 / WIDTH;
    localparam integer AW = $clog2(DEPTH);
    localparam integer KW = $clog2(DEPTH + 1);
    localparam integer L = @LATENCY@;

    reg clk = 0;
    always #5 clk = ~clk;
    reg reset = 1, start = 0, a_we = 0, b_we = 0;
    reg [AW-1:0] wr_addr = 0, base_addr = 0;
    reg [KW-1:0] k_length = 0;
    reg [WIDTH-1:0] a_wdata = 0, b_wdata = 0;
    integer integer_mode = 0;
    wire busy, done, result_valid;
    wire signed [19:0] result_data;

    orcus_inference_tile #(.LANES(LANES)) dut (
        .clk(clk), .reset(reset), .wr_addr(wr_addr),
        .a_we(a_we), .b_we(b_we), .a_wdata(a_wdata), .b_wdata(b_wdata),
        .start(start), .base_addr(base_addr), .k_length(k_length),
        .busy(busy), .done(done), .result_valid(result_valid),
        .result_data(result_data)
    );

    function [WIDTH-1:0] pattern_a(input integer address);
        integer lane, value;
        begin
            pattern_a = 0;
            for (lane = 0; lane < LANES; lane = lane + 1) begin
                if (integer_mode == 1) value = 8;       // -8
                else if (integer_mode == 2) value = 7;  // +7
                else value = (address*3 + lane*5 + 8) & 15;
                pattern_a[lane*4 +: 4] = value[3:0];
            end
        end
    endfunction

    function [WIDTH-1:0] pattern_b(input integer address);
        integer lane, value;
        begin
            pattern_b = 0;
            for (lane = 0; lane < LANES; lane = lane + 1) begin
                if (integer_mode != 0) value = 8; // -8
                else value = (address*7 + lane*3 + 2) & 15;
                pattern_b[lane*4 +: 4] = value[3:0];
            end
        end
    endfunction

    function integer signed row_dot(input integer address);
        integer lane, av, bv, total;
        reg [WIDTH-1:0] aword, bword;
        begin
            aword = pattern_a(address);
            bword = pattern_b(address);
            total = 0;
            for (lane = 0; lane < LANES; lane = lane + 1) begin
                av = (aword >> (lane*4)) & 15;
                bv = (bword >> (lane*4)) & 15;
                if (av >= 8) av = av - 16;
                if (bv >= 8) bv = bv - 16;
                total = total + av*bv;
            end
            row_dot = total;
        end
    endfunction

    function integer signed mac_value(input integer base, input integer count);
        integer row, total;
        begin
            total = 0;
            for (row = 0; row < count; row = row + 1)
                total = total + row_dot(base + row);
            mac_value = total;
        end
    endfunction

    task write_all_rows;
        integer address;
        begin
            for (address = 0; address < DEPTH; address = address + 1) begin
                @(negedge clk);
                wr_addr = address;
                a_wdata = pattern_a(address);
                b_wdata = pattern_b(address);
                a_we = 1; b_we = 1;
                @(posedge clk); #1;
                a_we = 0; b_we = 0;
            end
        end
    endtask

    task run_request(input integer base, input integer count);
        integer expected, timeout;
        begin
            expected = mac_value(base, count);
            @(negedge clk);
            base_addr = base; k_length = count; start = 1;
            @(posedge clk); #1;
            start = 0;
            timeout = 0;
            while ((result_valid !== 1'b1) && (timeout < DEPTH+L+20)) begin
                @(posedge clk); #1;
                timeout = timeout + 1;
            end
            if (result_valid !== 1'b1)
                $fatal(1, "timeout LANES=%0d base=%0d K=%0d", LANES, base, count);
            if (done !== 1'b1)
                $fatal(1, "done/result_valid mismatch LANES=%0d", LANES);
            if ($signed(result_data) !== expected)
                $fatal(1, "MAC mismatch LANES=%0d base=%0d K=%0d got=%0d expected=%0d",
                       LANES, base, count, $signed(result_data), expected);
            if (busy !== 1'b0)
                $fatal(1, "busy did not clear on completion LANES=%0d", LANES);
        end
    endtask

    task test_completion_edge_chain;
        integer first_expected, second_expected, wait_cycles, timeout;
        begin
            first_expected = mac_value(2, 2);
            second_expected = mac_value(9, 1);
            @(negedge clk);
            base_addr = 2; k_length = 2; start = 1;
            @(posedge clk); #1;
            start = 0;
            // Present the replacement request before the exact completion edge.
            for (wait_cycles = 1; wait_cycles < (2+L+1); wait_cycles = wait_cycles+1) begin
                @(posedge clk); #1;
            end
            @(negedge clk);
            base_addr = 9; k_length = 1; start = 1;
            @(posedge clk); #1;
            start = 0;
            if (result_valid !== 1'b1 || $signed(result_data) !== first_expected)
                $fatal(1, "first chained result incorrect LANES=%0d got=%0d expected=%0d",
                       LANES, $signed(result_data), first_expected);
            if (busy !== 1'b1)
                $fatal(1, "completion-edge chained request was not accepted");
            timeout = 0;
            while ((result_valid !== 1'b1 || $signed(result_data) !== second_expected) &&
                   (timeout < L+8)) begin
                @(posedge clk); #1;
                timeout = timeout + 1;
            end
            if (result_valid !== 1'b1 || $signed(result_data) !== second_expected)
                $fatal(1, "second chained result incorrect LANES=%0d got=%0d expected=%0d",
                       LANES, $signed(result_data), second_expected);
        end
    endtask

    integer i, base, count, timeout;
    initial begin
        @(posedge clk); #1;
        @(negedge clk); reset = 0;
        write_all_rows();

        // 512 back-to-back memory-fed transactions for each lane geometry.
        for (i = 0; i < 512; i = i + 1) begin
            base = (i*13) % (DEPTH-8);
            count = 1 + (i % 7);
            run_request(base, count);
        end
        run_request(0, DEPTH);

        // Reset during an active full-depth burst must flush all in-flight work.
        @(negedge clk); base_addr = 0; k_length = DEPTH; start = 1;
        @(posedge clk); #1; start = 0;
        repeat (5) begin @(posedge clk); #1; end
        @(negedge clk); reset = 1;
        @(posedge clk); #1;
        if (busy || result_valid || done)
            $fatal(1, "reset failed to flush active transaction LANES=%0d", LANES);
        @(negedge clk); reset = 0;
        repeat (L+4) begin
            @(posedge clk); #1;
            if (result_valid || done)
                $fatal(1, "stale completion after reset LANES=%0d", LANES);
        end

        // Invalid K=0 and end-exclusive wrap requests must be rejected.
        @(negedge clk); base_addr = 0; k_length = 0; start = 1;
        @(posedge clk); #1;
        if (busy || result_valid || done)
            $fatal(1, "K=0 request was not rejected LANES=%0d", LANES);
        @(negedge clk); start = 0; base_addr = DEPTH-1; k_length = 2; start = 1;
        @(posedge clk); #1;
        if (busy || result_valid || done)
            $fatal(1, "wrapping request was not rejected LANES=%0d", LANES);
        @(negedge clk); start = 0;

        test_completion_edge_chain();

        // Exhaustive signed extrema at full depth: +262144 and -229376.
        integer_mode = 1;
        write_all_rows();
        run_request(0, DEPTH);
        if ($signed(result_data) !== (64*LANES*DEPTH))
            $fatal(1, "positive full-depth extremum mismatch LANES=%0d", LANES);

        integer_mode = 2;
        write_all_rows();
        run_request(0, DEPTH);
        if ($signed(result_data) !== (-56*LANES*DEPTH))
            $fatal(1, "negative full-depth extremum mismatch LANES=%0d", LANES);

        $display("PASS inference tile LANES=%0d DEPTH=%0d L=%0d TX=512",
                 LANES, DEPTH, L);
        $finish;
    end
endmodule
"""
    return (template.replace("@LANES@", str(lanes))
            .replace("@LATENCY@", str(cfg.core_latency_cycles)))


@unittest.skipUnless(shutil.which("iverilog") and shutil.which("vvp"),
                     "Icarus Verilog is required")
class InferenceTileRTLTests(unittest.TestCase):
    def test_all_geometries_memory_fed_transactions_and_extrema(self) -> None:
        for lanes in (4, 8, 16, 32):
            with self.subTest(lanes=lanes), tempfile.TemporaryDirectory() as temp:
                temp_path = Path(temp)
                generated = temp_path / f"orcus_dadda_pipelined_{lanes}.sv"
                tb_path = temp_path / "tb.sv"
                sim_path = temp_path / "sim.out"
                generated.write_text(generate_dadda_pipelined(lanes).source)
                tb_path.write_text(_testbench(lanes))
                compile_result = subprocess.run(
                    ["iverilog", "-g2012", "-s", "tb", "-o", str(sim_path),
                     str(SCRATCHPAD), str(CONTROLLER), str(RTL),
                     str(generated), str(tb_path)],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(compile_result.returncode, 0,
                                 compile_result.stdout + compile_result.stderr)
                run_result = subprocess.run(
                    ["vvp", str(sim_path)], cwd=ROOT, text=True,
                    capture_output=True, check=False, timeout=180,
                )
                self.assertEqual(run_result.returncode, 0,
                                 run_result.stdout + run_result.stderr)
                self.assertIn(f"PASS inference tile LANES={lanes}", run_result.stdout)

    def test_twenty_bit_accumulator_bounds_for_every_geometry(self) -> None:
        minimum, maximum = signed_bounds(20)
        for lanes in (4, 8, 16, 32):
            cfg = TileConfig(lanes)
            lower, upper = mac_accumulator_bounds(lanes, cfg.maximum_k)
            self.assertEqual((lower, upper), (-229_376, 262_144))
            self.assertGreaterEqual(lower, minimum)
            self.assertLessEqual(upper, maximum)
            self.assertEqual(cfg.accumulator_width_bits, 20)


if __name__ == "__main__":
    unittest.main()
