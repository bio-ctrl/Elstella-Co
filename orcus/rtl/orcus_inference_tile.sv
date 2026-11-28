// ORCUS M4.4 — integrated signed-INT4 inference tile.
// Reuses the validated M4.2 memories, M4.3 K-loop controller, and generated
// M3 Dadda cores without modifying their internals.
module orcus_inference_tile #(
    parameter integer LANES = 32
) (
    input  wire clk,
    input  wire reset, // synchronous active-high
    input  wire [$clog2(16384/(LANES*4))-1:0] wr_addr,
    input  wire a_we,
    input  wire b_we,
    input  wire [LANES*4-1:0] a_wdata,
    input  wire [LANES*4-1:0] b_wdata,
    input  wire start,
    input  wire [$clog2(16384/(LANES*4))-1:0] base_addr,
    input  wire [$clog2(16384/(LANES*4)+1)-1:0] k_length,
    output wire busy,
    output reg done,
    output reg result_valid,
    output reg signed [19:0] result_data
);
    localparam integer MEMORY_BITS = 16384;
    localparam integer WORD_WIDTH = LANES * 4;
    localparam integer DEPTH = MEMORY_BITS / WORD_WIDTH;
    localparam integer ADDR_WIDTH = $clog2(DEPTH);
    localparam integer K_WIDTH = $clog2(DEPTH + 1);
    localparam integer DOT_WIDTH = 8 + $clog2(LANES);
    localparam integer CORE_LATENCY =
        (LANES == 4) ? 6 :
        (LANES == 8) ? 8 :
        (LANES == 16) ? 10 : 11;

    wire mem_rd_en;
    wire [ADDR_WIDTH-1:0] mem_rd_addr;
    wire [WORD_WIDTH-1:0] a_rd_data, b_rd_data;
    wire first_tag, last_tag, valid_tag;
    wire pipeline_done;
    wire core_valid;
    wire signed [DOT_WIDTH-1:0] core_data;
    wire signed [19:0] core_data_ext =
        {{(20-DOT_WIDTH){core_data[DOT_WIDTH-1]}}, core_data};

    // The memory and controller register read data and tags on the same edge.
    // The Dadda core samples those registered values on the following edge.
    orcus_operand_scratchpads #(.LANES(LANES)) u_memories (
        .clk(clk), .wr_addr(wr_addr), .a_we(a_we), .b_we(b_we),
        .a_wdata(a_wdata), .b_wdata(b_wdata),
        .rd_en(mem_rd_en), .rd_addr(mem_rd_addr),
        .a_rd_data(a_rd_data), .b_rd_data(b_rd_data)
    );

    orcus_kloop_controller #(.LANES(LANES)) u_controller (
        .clk(clk), .reset(reset), .start(start),
        .base_addr(base_addr), .k_length(k_length),
        .wr_addr(wr_addr), .a_we(a_we), .b_we(b_we),
        .pipeline_done(pipeline_done), .busy(busy),
        .rd_en(mem_rd_en), .rd_addr(mem_rd_addr),
        .first(first_tag), .last(last_tag), .valid(valid_tag)
    );

    // The Dadda generator has L reduction-register stages plus a separately
    // registered out_valid boundary. The accumulator samples its outputs on
    // the next edge, so companion tags use L+1 registers as well.
    reg [CORE_LATENCY:0] tag_valid_pipe;
    reg [CORE_LATENCY:0] tag_first_pipe;
    reg [CORE_LATENCY:0] tag_last_pipe;

    always @(posedge clk) begin
        if (reset) begin
            tag_valid_pipe <= {(CORE_LATENCY+1){1'b0}};
            tag_first_pipe <= {(CORE_LATENCY+1){1'b0}};
            tag_last_pipe  <= {(CORE_LATENCY+1){1'b0}};
        end else begin
            tag_valid_pipe <= {tag_valid_pipe[CORE_LATENCY-1:0], valid_tag};
            tag_first_pipe <= {tag_first_pipe[CORE_LATENCY-1:0], first_tag};
            tag_last_pipe  <= {tag_last_pipe[CORE_LATENCY-1:0], last_tag};
        end
    end

    generate
        if (LANES == 4) begin : g_dadda_4
            orcus_dadda_pipelined_4 u_core (
                .clk(clk), .reset(reset), .in_valid(valid_tag),
                .out_valid(core_valid), .out_data(core_data),
                .a0(a_rd_data[3:0]), .b0(b_rd_data[3:0]),
                .a1(a_rd_data[7:4]), .b1(b_rd_data[7:4]),
                .a2(a_rd_data[11:8]), .b2(b_rd_data[11:8]),
                .a3(a_rd_data[15:12]), .b3(b_rd_data[15:12])
            );
        end else if (LANES == 8) begin : g_dadda_8
            orcus_dadda_pipelined_8 u_core (
                .clk(clk), .reset(reset), .in_valid(valid_tag),
                .out_valid(core_valid), .out_data(core_data),
                .a0(a_rd_data[3:0]), .b0(b_rd_data[3:0]),
                .a1(a_rd_data[7:4]), .b1(b_rd_data[7:4]),
                .a2(a_rd_data[11:8]), .b2(b_rd_data[11:8]),
                .a3(a_rd_data[15:12]), .b3(b_rd_data[15:12]),
                .a4(a_rd_data[19:16]), .b4(b_rd_data[19:16]),
                .a5(a_rd_data[23:20]), .b5(b_rd_data[23:20]),
                .a6(a_rd_data[27:24]), .b6(b_rd_data[27:24]),
                .a7(a_rd_data[31:28]), .b7(b_rd_data[31:28])
            );
        end else if (LANES == 16) begin : g_dadda_16
            orcus_dadda_pipelined_16 u_core (
                .clk(clk), .reset(reset), .in_valid(valid_tag),
                .out_valid(core_valid), .out_data(core_data),
                .a0(a_rd_data[3:0]), .b0(b_rd_data[3:0]),
                .a1(a_rd_data[7:4]), .b1(b_rd_data[7:4]),
                .a2(a_rd_data[11:8]), .b2(b_rd_data[11:8]),
                .a3(a_rd_data[15:12]), .b3(b_rd_data[15:12]),
                .a4(a_rd_data[19:16]), .b4(b_rd_data[19:16]),
                .a5(a_rd_data[23:20]), .b5(b_rd_data[23:20]),
                .a6(a_rd_data[27:24]), .b6(b_rd_data[27:24]),
                .a7(a_rd_data[31:28]), .b7(b_rd_data[31:28]),
                .a8(a_rd_data[35:32]), .b8(b_rd_data[35:32]),
                .a9(a_rd_data[39:36]), .b9(b_rd_data[39:36]),
                .a10(a_rd_data[43:40]), .b10(b_rd_data[43:40]),
                .a11(a_rd_data[47:44]), .b11(b_rd_data[47:44]),
                .a12(a_rd_data[51:48]), .b12(b_rd_data[51:48]),
                .a13(a_rd_data[55:52]), .b13(b_rd_data[55:52]),
                .a14(a_rd_data[59:56]), .b14(b_rd_data[59:56]),
                .a15(a_rd_data[63:60]), .b15(b_rd_data[63:60])
            );
        end else if (LANES == 32) begin : g_dadda_32
            orcus_dadda_pipelined_32 u_core (
                .clk(clk), .reset(reset), .in_valid(valid_tag),
                .out_valid(core_valid), .out_data(core_data),
                .a0(a_rd_data[3:0]), .b0(b_rd_data[3:0]),
                .a1(a_rd_data[7:4]), .b1(b_rd_data[7:4]),
                .a2(a_rd_data[11:8]), .b2(b_rd_data[11:8]),
                .a3(a_rd_data[15:12]), .b3(b_rd_data[15:12]),
                .a4(a_rd_data[19:16]), .b4(b_rd_data[19:16]),
                .a5(a_rd_data[23:20]), .b5(b_rd_data[23:20]),
                .a6(a_rd_data[27:24]), .b6(b_rd_data[27:24]),
                .a7(a_rd_data[31:28]), .b7(b_rd_data[31:28]),
                .a8(a_rd_data[35:32]), .b8(b_rd_data[35:32]),
                .a9(a_rd_data[39:36]), .b9(b_rd_data[39:36]),
                .a10(a_rd_data[43:40]), .b10(b_rd_data[43:40]),
                .a11(a_rd_data[47:44]), .b11(b_rd_data[47:44]),
                .a12(a_rd_data[51:48]), .b12(b_rd_data[51:48]),
                .a13(a_rd_data[55:52]), .b13(b_rd_data[55:52]),
                .a14(a_rd_data[59:56]), .b14(b_rd_data[59:56]),
                .a15(a_rd_data[63:60]), .b15(b_rd_data[63:60]),
                .a16(a_rd_data[67:64]), .b16(b_rd_data[67:64]),
                .a17(a_rd_data[71:68]), .b17(b_rd_data[71:68]),
                .a18(a_rd_data[75:72]), .b18(b_rd_data[75:72]),
                .a19(a_rd_data[79:76]), .b19(b_rd_data[79:76]),
                .a20(a_rd_data[83:80]), .b20(b_rd_data[83:80]),
                .a21(a_rd_data[87:84]), .b21(b_rd_data[87:84]),
                .a22(a_rd_data[91:88]), .b22(b_rd_data[91:88]),
                .a23(a_rd_data[95:92]), .b23(b_rd_data[95:92]),
                .a24(a_rd_data[99:96]), .b24(b_rd_data[99:96]),
                .a25(a_rd_data[103:100]), .b25(b_rd_data[103:100]),
                .a26(a_rd_data[107:104]), .b26(b_rd_data[107:104]),
                .a27(a_rd_data[111:108]), .b27(b_rd_data[111:108]),
                .a28(a_rd_data[115:112]), .b28(b_rd_data[115:112]),
                .a29(a_rd_data[119:116]), .b29(b_rd_data[119:116]),
                .a30(a_rd_data[123:120]), .b30(b_rd_data[123:120]),
                .a31(a_rd_data[127:124]), .b31(b_rd_data[127:124])
            );
        end
    endgenerate

    wire final_tag_valid = tag_valid_pipe[CORE_LATENCY] && core_valid;
    wire final_tag_first = tag_first_pipe[CORE_LATENCY];
    wire final_tag_last  = tag_last_pipe[CORE_LATENCY];
    wire signed [19:0] accumulated_next =
        final_tag_first ? core_data_ext : (result_data + core_data_ext);

    // Controller completion and accumulator commit share the same edge.
    assign pipeline_done = final_tag_valid && final_tag_last;

    always @(posedge clk) begin
        if (reset) begin
            result_data  <= 20'sd0;
            result_valid <= 1'b0;
            done         <= 1'b0;
        end else begin
            result_valid <= 1'b0;
            done         <= 1'b0;
            if (final_tag_valid) begin
                if (final_tag_first)
                    result_data <= core_data_ext;
                else
                    result_data <= result_data + core_data_ext;

                if (final_tag_last) begin
                    result_valid <= 1'b1;
                    done <= 1'b1;
                    result_data <= accumulated_next;
                end
            end
        end
    end

`ifndef SYNTHESIS
    initial begin
        if (!(LANES == 4 || LANES == 8 || LANES == 16 || LANES == 32))
            $fatal(1, "ORCUS inference tile LANES must be 4, 8, 16, or 32");
        if (DEPTH * WORD_WIDTH != MEMORY_BITS)
            $fatal(1, "ORCUS inference tile must contain 16384 bits per operand");
        if (DOT_WIDTH > 20)
            $fatal(1, "ORCUS dot width cannot exceed 20-bit accumulator");
    end
`endif
endmodule
