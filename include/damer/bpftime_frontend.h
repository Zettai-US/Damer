#ifndef DAMER_BPFTIME_FRONTEND_H
#define DAMER_BPFTIME_FRONTEND_H

/*
 * Minimal C ABI shared by bpftime eBPF frontends and the Damer optimizer.
 *
 * bpftime loads and runs the eBPF program. The program emits typed movement
 * events through these helpers. Damer consumes the events as switchlets and
 * optimizes placement, fusion, and scheduling separately.
 */

#ifndef SEC
#define SEC(name) __attribute__((section(name), used))
#endif

typedef unsigned char damer_u8;
typedef unsigned short damer_u16;
typedef unsigned int damer_u32;
typedef unsigned long long damer_u64;

enum damer_node_kind {
  DAMER_NODE_UNKNOWN = 0,
  DAMER_NODE_HOST_MEMORY = 1,
  DAMER_NODE_CXL_MEMORY_DEVICE = 2,
  DAMER_NODE_GPU_MEMORY = 3,
  DAMER_NODE_ACCELERATOR = 4,
  DAMER_NODE_SWITCH_COMPUTE_ENGINE = 5,
};

enum damer_transform_flags {
  DAMER_TRANSFORM_NONE = 0,
  DAMER_TRANSFORM_QUANTIZE = 1u << 0,
  DAMER_TRANSFORM_COMPRESS = 1u << 1,
  DAMER_TRANSFORM_CHECKSUM = 1u << 2,
  DAMER_TRANSFORM_FILTER = 1u << 3,
  DAMER_TRANSFORM_REDUCE = 1u << 4,
  DAMER_TRANSFORM_SCATTER_GATHER = 1u << 5,
  DAMER_TRANSFORM_REPLICATE = 1u << 6,
  DAMER_TRANSFORM_PERSIST = 1u << 7,
};

enum damer_ordering_requirement {
  DAMER_ORDER_NONE = 0,
  DAMER_ORDER_PROGRAM = 1,
  DAMER_ORDER_ACQUIRE_RELEASE = 2,
  DAMER_ORDER_TOTAL = 3,
};

enum damer_ownership {
  DAMER_OWNERSHIP_UNKNOWN = 0,
  DAMER_OWNERSHIP_BORROWED = 1,
  DAMER_OWNERSHIP_SOURCE = 2,
  DAMER_OWNERSHIP_DESTINATION = 3,
  DAMER_OWNERSHIP_SHARED = 4,
};

struct damer_bpf_ctx {
  damer_u64 switchlet_id;
  damer_u64 event_id;
  damer_u64 tenant_id;
  damer_u64 stream_id;
};

struct damer_edge_event {
  damer_u32 source_node;
  damer_u32 destination_node;
  damer_u32 transformations;
  damer_u32 ordering;
  damer_u32 ownership;
  damer_u32 alias_set;
  damer_u64 bytes;
  damer_u64 stride;
  damer_u64 reuse_distance;
  damer_u32 read_ratio;
  damer_u32 write_ratio;
  damer_u64 source_addr;
  damer_u64 destination_addr;
};

struct damer_decision {
  damer_u32 placement_node;
  damer_u32 action_mask;
  damer_u32 priority;
  damer_u32 ttl;
};

#ifndef DAMER_HELPER_ID_EMIT_EDGE
#define DAMER_HELPER_ID_EMIT_EDGE 9001
#endif

#ifndef DAMER_HELPER_ID_SUBMIT_DECISION
#define DAMER_HELPER_ID_SUBMIT_DECISION 9002
#endif

typedef long (*damer_emit_edge_fn)(struct damer_bpf_ctx *ctx,
                                   const struct damer_edge_event *edge);
typedef long (*damer_submit_decision_fn)(struct damer_bpf_ctx *ctx,
                                         const struct damer_decision *decision);

static damer_emit_edge_fn damer_emit_edge =
    (damer_emit_edge_fn)(void *)DAMER_HELPER_ID_EMIT_EDGE;

static damer_submit_decision_fn damer_submit_decision =
    (damer_submit_decision_fn)(void *)DAMER_HELPER_ID_SUBMIT_DECISION;

#endif /* DAMER_BPFTIME_FRONTEND_H */
