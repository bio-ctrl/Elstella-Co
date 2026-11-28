"""Cycle-accurate M4.4 integration tests for the complete ORCUS inference tile."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orcus.dadda_pipeline import generate_dadda_pipelined
from orcus.inference_tile import (
    TileConfig, mac_accumulator_bounds, signed_bounds, signed_int4_mac_accumulate,
)

ROOT = Path(__file__).resolve().parents[1]
RTL = ROOT / "orcus" / "rtl" / "orcus_inference_tile.sv"
SCRATCHPAD = ROOT / "orcus" / "rtl" / "orcus_operand_scratchpad.sv"
CONTROLLER = ROOT / "orcus" / "rtl" / "orcus_kloop_controller.sv"


def _golden_row(address: int, lanes: int, operand: str, mode: int = 0) -> tuple[int, ...]:
    if mode == 1:
        return (-8,) * lanes
    if mode == 2 and operand == "a":
        return (7,) * lanes
    if mode == 2 and operand == "b":
        return (-8,) * lanes
    multiplier, offset = ((3, 8) if operand == "a" else (7, 2))
    lane_multiplier = 5 if operand == "a" else 3
    values = []
    for lane in range(lanes):
        nibble = (address * multiplier + lane * lane_multiplier + offset) & 15
        values.append(nibble - 16 if nibble >= 8 else nibble)
    return tuple(values)


def _golden_mac(base: int, count: int, lanes: int, mode: int = 0) -> int:
    a_rows = [_golden_row(base + row, lanes, "a", mode) for row in range(count)]
    b_rows = [_golden_row(base + row, lanes, "b", mode) for row in range(count)]
    return signed_int4_mac_accumulate(a_rows, b_rows, lanes)


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
    reg signed [31:0] expected_vectors [0:511];
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

    task run_request(input integer base, input integer count, input integer expected);
        integer timeout;
        begin
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
            first_expected = @CHAIN_FIRST_EXPECTED@;
            second_expected = @CHAIN_SECOND_EXPECTED@;
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
        @EXPECTED_VECTOR_INIT@

        // 512 back-to-back memory-fed transactions for each lane geometry.
        for (i = 0; i < 512; i = i + 1) begin
            base = (i*13) % (DEPTH-8);
            count = 1 + (i % 7);
            run_request(base, count, expected_vectors[i]);
        end
        run_request(0, DEPTH, @FULL_MODE0_EXPECTED@);

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
        run_request(0, DEPTH, @FULL_MODE1_EXPECTED@);
        if ($signed(result_data) !== (64*LANES*DEPTH))
            $fatal(1, "positive full-depth extremum mismatch LANES=%0d", LANES);

        integer_mode = 2;
        write_all_rows();
        run_request(0, DEPTH, @FULL_MODE2_EXPECTED@);
        if ($signed(result_data) !== (-56*LANES*DEPTH))
            $fatal(1, "negative full-depth extremum mismatch LANES=%0d", LANES);

        $display("PASS inference tile LANES=%0d DEPTH=%0d L=%0d TX=512",
                 LANES, DEPTH, L);
        $finish;
    end
endmodule
"""
    expected_init = "\n".join(
        f"        expected_vectors[{i}] = {_golden_mac((i*13) % (cfg.memory_depth_words-8), 1+(i%7), lanes)};"
        for i in range(512)
    )
    template = template.replace("@EXPECTED_VECTOR_INIT@", expected_init)
    template = template.replace("@CHAIN_FIRST_EXPECTED@", str(_golden_mac(2, 2, lanes)))
    template = template.replace("@CHAIN_SECOND_EXPECTED@", str(_golden_mac(9, 1, lanes)))
    template = template.replace("@FULL_MODE0_EXPECTED@", str(_golden_mac(0, cfg.maximum_k, lanes)))
    template = template.replace("@FULL_MODE1_EXPECTED@", str(_golden_mac(0, cfg.maximum_k, lanes, 1)))
    template = template.replace("@FULL_MODE2_EXPECTED@", str(_golden_mac(0, cfg.maximum_k, lanes, 2)))
    return (template.replace("@LANES@", str(lanes))
            .replace("@LATENCY@", str(cfg.core_latency_cycles)))


@unittest.skipUnless(shutil.which("iverilog") and shutil.which("vvp"),
                     "Icarus Verilog is required")
class InferenceTileRTLTests(unittest.TestCase):
    def test_all_geometries_memory_fed_transactions_and_extrema(self) -> None:
        reports = []
        for lanes in (4, 8, 16, 32):
            cfg = TileConfig(lanes)
            expected_vectors = [
                _golden_mac((i*13) % (cfg.memory_depth_words-8), 1+(i%7), lanes)
                for i in range(512)
            ]
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
                reports.append({
                    "lanes": lanes,
                    "depth": cfg.maximum_k,
                    "memory_fed_transactions": 512,
                    "golden_oracle": "orcus.inference_tile.signed_int4_mac_accumulate",
                    "expected_vector_sha256": hashlib.sha256(
                        json.dumps(expected_vectors, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "full_depth_mode0_expected": _golden_mac(0, cfg.maximum_k, lanes),
                    "full_depth_positive_extremum_expected": _golden_mac(0, cfg.maximum_k, lanes, 1),
                    "full_depth_negative_extremum_expected": _golden_mac(0, cfg.maximum_k, lanes, 2),
                    "simulation_stdout": run_result.stdout.strip(),
                })
        evidence_dir = Path(tempfile.gettempdir()) / "orcus-inference-tile-m4-4"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "results.json").write_text(json.dumps({
            "schema": "orcus-m4-4-inference-tile-regression-v1",
            "status": "PASS",
            "geometries": reports,
        }, indent=2) + "\n")

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
