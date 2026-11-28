# ORCUS MAC + Local-Memory Inference Tile v1 — M4.1 Contract

**Status:** M4.1 specification and Python reference model. No tile RTL or scratchpad RTL is implemented in this milestone.

## 1. Scope and baseline

M4.1 defines the signed-INT4 arithmetic reference, fixed parameter relationships, packed-word convention, and host/compute interface contract. The validated Run #267 pipelined Dadda row-dot-product core is reused unchanged in later milestones. It accepts one row of signed INT4 A/B operands per cycle and has execution-verified latency L = 6 / 8 / 10 / 11 cycles for 4 / 8 / 16 / 32 lanes.

The inference tile adds two local operand memories and a K-row full-precision accumulator. The local memories, controller, and accumulator are not implemented in M4.1.

## 2. Fixed v1 parameters

Each operand scratchpad contains exactly 16,384 logical bits (2 KiB). A word stores N signed INT4 values.

| Lanes N | Word width | Depth | Dot width W_DOT | Core latency L | Address width | K-length width | Accumulator width W_ACC |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 16 bits | 1,024 | 10 bits | 6 | 10 bits | 11 bits | 20 bits |
| 8 | 32 bits | 512 | 11 bits | 8 | 9 bits | 10 bits | 20 bits |
| 16 | 64 bits | 256 | 12 bits | 10 | 8 bits | 9 bits | 20 bits |
| 32 | 128 bits | 128 | 13 bits | 11 | 7 bits | 8 bits | 20 bits |

There are two independent scratchpads, A and B: 32,768 logical bits total (4 KiB), or 4,096 INT4 values per operand.

- W_DOT = 8 + ceil(log2(N)).
- K_MAX = memory depth = 16,384 / (4N).
- W_ACC = W_DOT + ceil(log2(K_MAX)) = 20 bits for all supported N.
- result_data is signed 20-bit two's-complement.
- The memory-depth and width relationships are assertions, not synthesis evidence. DP16KD inference and packing are verified in M4.2/M4.5.

## 3. Signed arithmetic reference

Each operand element is a signed two's-complement INT4 value in [-8, 7]. For row r:

    dot(r) = sum(A[r][i] * B[r][i], i = 0 .. N-1)

One product is in [-56, 64], so a row dot product is bounded by:

    -56N <= dot(r) <= 64N

For a request of K rows:

    result = sum(dot(r), r = 0 .. K-1)
    -56NK <= result <= 64NK

At maximum depth, N*K_MAX = 4,096 for every supported configuration. Therefore the full result range is [-229,376, 262,144], which fits in signed 20-bit range [-524,288, 524,287]. V1 performs no truncation, saturation, or rounding in the accumulator.

The Python reference model in orcus/inference_tile.py is the M4.1 golden oracle. It validates lane count, row length, signed INT4 range, K bounds, exact arithmetic, and result-width fit.

## 4. Packed operand word convention

For an N-lane word, lane 0 occupies bits [3:0], lane 1 occupies [7:4], and lane i occupies bits [4i+3:4i]. Each nibble is two's-complement signed INT4. The least-significant nibble is lane 0.

The host write interface uses independent enables for A and B:

- wr_addr: address of the word to write.
- a_we / b_we: independent write enables.
- a_wdata / b_wdata: N*4-bit packed operand words.

The host may write only while busy is low. Compute reads and host writes are not allowed to collide at the same address. V1 does not define read-during-write behavior.

## 5. Compute interface

| Signal | Direction | Width / type | Contract |
|---|---|---|---|
| clk | input | 1 | Rising-edge clock |
| reset | input | 1 | Synchronous active-high reset |
| wr_addr | input | ceil(log2(depth)) | Host write address |
| a_we, b_we | input | 1 each | Independent operand write enables |
| a_wdata, b_wdata | input | N*4 each | Packed signed INT4 words |
| start | input | 1 | Request acceptance when idle |
| base_addr | input | ceil(log2(depth)) | First row address |
| k_length | input | ceil(log2(depth+1)) | Number of rows, 1 through depth |
| busy | output | 1 | High from accepted start through completion |
| done | output | 1 | One-cycle completion pulse |
| result_valid | output | 1 | One-cycle pulse with done |
| result_data | output | signed 20 | Full-precision accumulated result |

A request reads K consecutive addresses: base_addr through base_addr + K - 1. Address wrap is forbidden. The request must satisfy:

- 0 <= base_addr < depth
- 1 <= k_length <= depth
- base_addr + k_length <= depth

A start presented while busy is high is not accepted. Host writes are permitted only while idle. The v1 design has one accumulation context, no DMA, and no interleaved contexts.

On completion, done and result_valid assert together for one cycle. result_data holds the last completed result until the next completion or reset. Reset clears controller state, read/valid/tag pipelines, accumulator, done, busy, and result_valid. Scratchpad contents are not reset and are unspecified after power-up/reset.

Invalid requests and prohibited write/read collisions are outside the legal interface contract and must be caught by M4.1 software assertions and later RTL simulation assertions. The v1 interface has no error output.

## 6. Request-to-result latency correction

The earlier shorthand L+2 is only correct for a one-row request (K = 1). For a general K-row request, rows are issued one per cycle:

1. The first synchronous memory read is issued on the start-acceptance edge.
2. The final of K reads is issued K-1 cycles after acceptance.
3. The synchronous read contributes one cycle before the final row enters the Dadda core.
4. The Dadda core contributes L cycles.
5. The accumulator result register contributes one cycle.

Under this edge-count convention, the target is:

    request_to_result_cycles(K) = (K - 1) + 1 + L + 1 = K + L + 1

For K = 1 this reduces to L + 2. For 32 lanes, L = 11, so the target is 13 cycles for K=1 and 140 cycles for K=128. These are derived design targets, not measured RTL behavior. M4.3/M4.4 must prove the exact edge alignment with cycle-accurate tests and formal checks before any latency claim is promoted to verified evidence.

## 7. M4.1 acceptance checklist

- [x] Pure-Python signed INT4 row-dot-product and K-row MAC reference.
- [x] Fixed memory dimensions, W_DOT/W_ACC, address widths, K widths, and Run #267 core latency constants.
- [x] Packed INT4 nibble ordering and round-trip helpers.
- [x] Interface contract, reset/write/collision/address semantics, and latency derivation.
- [ ] Run unit tests on the exact branch head.
- [ ] Open a review PR and obtain an exact-head CI pass.

No memory RTL is part of M4.1. M4.2 implements the A/B synchronous true-dual-port scratchpads and proves DP16KD inference.
