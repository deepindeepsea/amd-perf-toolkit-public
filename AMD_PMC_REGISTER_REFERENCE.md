# AMD PMC Physical Register Reference

## 📋 **PMC Register Mnemonic Format**

According to AMD official documentation, the x86 performance monitor counter physical register mnemonic format for Zen 4/5 architectures is:

- **PMCxXXX**: Core Performance Monitor Counters
- **L2IPMCxXXX**: L2 Instruction Performance Monitor Counters (if applicable)

Where **XXX** is the performance monitor select value.

**Note**: Modern AMD Zen architectures have integrated memory controllers and don't use the traditional northbridge design, so NBPMCxXXX registers are not applicable to Zen 4/5 CPUs.

## 🎯 **Core PMC Registers (PMCxXXX)**

### **PMCx0C0 - PMCx0C3: Retired Instruction Events**
| **Register** | **Mnemonic** | **Description** | **perf Event Name** |
|--------------|--------------|-----------------|-------------------|
| `PMCx0C0` | ex_ret_instr | Total retired instructions | `instructions` |
| `PMCx0C1` | ex_ret_ops | Total retired micro-ops | `cycles` |
| `PMCx0C2` | ex_ret_brn | Total retired branches | `branches` |
| `PMCx0C3` | ex_ret_brn_misp | Retired branch mispredictions | `branch-misses` |

### **PMCx003: Floating-Point Operations**
| **Register** | **Bit Mask** | **Description** | **perf Event Name** |
|--------------|--------------|-----------------|-------------------|
| `PMCx003[0]` | 0x01 | Add/subtract FLOPs | `fp_ret_sse_avx_ops.add_sub_flops` |
| `PMCx003[1]` | 0x02 | Multiply FLOPs | `fp_ret_sse_avx_ops.mult_flops` |
| `PMCx003[2]` | 0x04 | Division/sqrt FLOPs | `fp_ret_sse_avx_ops.div_flops` |
| `PMCx003[3]` | 0x08 | Multiply-accumulate FLOPs | `fp_ret_sse_avx_ops.mac_flops` |

### **PMCx043-PMCx044: Data Cache Fill Events**
| **Register** | **Bit Mask** | **Description** | **perf Event Name** |
|--------------|--------------|-----------------|-------------------|
| `PMCx043[6]` | 0x40 | Demand fills from L2 | `ls_dmnd_fills_from_sys.local_l2` |
| `PMCx043[3]` | 0x08 | Demand fills from memory | `ls_dmnd_fills_from_sys.local_mem` |
| `PMCx044[6]` | 0x40 | All fills from L2 | `ls_any_fills_from_sys.local_l2` |

### **PMCx045: L1 Data TLB Miss Events**
| **Register** | **Bit Mask** | **Description** | **perf Event Name** |
|--------------|--------------|-----------------|-------------------|
| `PMCx045[0]` | 0x01 | All L2 TLB hits | `ls_l1_d_tlb_miss.all_l2_hit` |
| `PMCx045[1]` | 0x02 | All L2 TLB misses | `ls_l1_d_tlb_miss.all_l2_miss` |
| `PMCx045[2]` | 0x04 | 4KB page TLB hit in L2 | `ls_l1_d_tlb_miss.tlb_reload_4k_l2_hit` |
| `PMCx045[3]` | 0x08 | 4KB page TLB miss | `ls_l1_d_tlb_miss.tlb_reload_4k_l2_miss` |
| `PMCx045[4]` | 0x10 | 2MB page TLB hit in L2 | `ls_l1_d_tlb_miss.tlb_reload_2m_l2_hit` |
| `PMCx045[5]` | 0x20 | 2MB page TLB miss | `ls_l1_d_tlb_miss.tlb_reload_2m_l2_miss` |
| `PMCx045[6]` | 0x40 | 1GB page TLB hit in L2 | `ls_l1_d_tlb_miss.tlb_reload_1g_l2_hit` |
| `PMCx045[7]` | 0x80 | 1GB page TLB miss | `ls_l1_d_tlb_miss.tlb_reload_1g_l2_miss` |

### **PMCx064: L2 Cache Request Statistics**
| **Register** | **Bit Mask** | **Description** | **perf Event Name** |
|--------------|--------------|-----------------|-------------------|
| `PMCx064[0]` | 0x01 | Instruction cache MISS | `l2_cache_req_stat.ic_fill_miss` |
| `PMCx064[1]` | 0x02 | Instruction cache hit (non-mod) | `l2_cache_req_stat.ic_fill_hit_s` |
| `PMCx064[2]` | 0x04 | Instruction cache hit (modifiable) | `l2_cache_req_stat.ic_fill_hit_x` |
| `PMCx064[3]` | 0x08 | **Data cache MISS in L2** | `l2_cache_req_stat.ls_rd_blk_c` |
| `PMCx064[4]` | 0x10 | Data cache store/state change | `l2_cache_req_stat.ls_rd_blk_x` |
| `PMCx064[5]` | 0x20 | Data cache read hit (non-mod) | `l2_cache_req_stat.ls_rd_blk_l_hit_s` |
| `PMCx064[6]` | 0x40 | Data cache read hit (modifiable) | `l2_cache_req_stat.ls_rd_blk_l_hit_x` |
| `PMCx064[7]` | 0x80 | Data cache shared read hit | `l2_cache_req_stat.ls_rd_blk_cs` |

## 🔧 **Raw Event Programming Examples**

### **Using Raw Register Format**
```bash
# PMCx0C0 - Retired instructions (basic event, no mask needed)
perf stat -e r00C0 # Raw event format - WORKS

# For events with unit masks, use symbolic names instead of raw hex:
# PMCx064 events - use symbolic names (raw hex with masks is complex)
perf stat -e l2_cache_req_stat.ls_rd_blk_c # Data cache misses - RECOMMENDED

# PMCx045 events - use symbolic names  
perf stat -e ls_l1_d_tlb_miss.all_l2_miss # TLB L2 misses - RECOMMENDED

# Raw hex format with unit masks is architecture-specific and complex
# Symbolic event names are more reliable and portable
```

### **Using Symbolic Event Names (Strongly Recommended)**
```bash
# PMCx0C0-0C3 - Core execution
perf stat -e instructions,cycles,branches,branch-misses

# PMCx064 - L2 cache analysis
perf stat -e l2_cache_req_stat.ls_rd_blk_l_hit_s,l2_cache_req_stat.ls_rd_blk_c

# PMCx045 - TLB analysis  
perf stat -e ls_l1_d_tlb_miss.all_l2_miss,ls_l1_d_tlb_miss.all_l2_hit

# PMCx003 - Floating-point operations
perf stat -e fp_ret_sse_avx_ops.add_sub_flops,fp_ret_sse_avx_ops.mult_flops
```

## 🚀 **Rails Workload Mapping to PMC Registers**

### **JSON Serialization Analysis**
**Target PMC Registers**: `PMCx064` (L2 cache), `PMCx045` (TLB)
```bash
# Focus on object traversal efficiency
perf stat -e l2_cache_req_stat.ls_rd_blk_l_hit_s,l2_cache_req_stat.ls_rd_blk_c,\
ls_l1_d_tlb_miss.all_l2_miss,ls_dmnd_fills_from_sys.local_l2 \
  wrk -t8 -c200 -d30s http://localhost:3000/json
```

### **Data Processing Analysis**  
**Target PMC Registers**: `PMCx003` (FP ops), `PMCx043` (data fills)
```bash
# Focus on arithmetic and memory bandwidth
perf stat -e fp_ret_sse_avx_ops.add_sub_flops,fp_ret_sse_avx_ops.mult_flops,\
ls_dmnd_fills_from_sys.local_mem,ls_any_fills_from_sys.local_l2 \
  wrk -t8 -c200 -d30s http://localhost:3000/data
```

### **Method Dispatch Analysis**
**Target PMC Registers**: `PMCx0C2-0C3` (branches), `PMCx064` (I-cache)
```bash
# Focus on Ruby method call efficiency
perf stat -e branches,branch-misses,\
l2_cache_req_stat.ic_fill_hit_s,l2_cache_req_stat.ic_fill_miss \
  wrk -t8 -c200 -d30s http://localhost:3000/hello
```

## 📊 **Performance Ratio Calculations**

### **L2 Cache Efficiency (PMCx064)**
```bash
# L2 data cache hit rate
L2_data_hit_rate = (ls_rd_blk_l_hit_s + ls_rd_blk_l_hit_x) / 
                   (ls_rd_blk_l_hit_s + ls_rd_blk_l_hit_x + ls_rd_blk_c)

# Target: >90% for Rails object-heavy workloads
```

### **TLB Efficiency (PMCx045)**
```bash
# TLB hit rate for virtual memory performance
TLB_hit_rate = all_l2_hit / (all_l2_hit + all_l2_miss)

# Target: >99% for well-tuned Ruby heap
```

### **Memory Bandwidth Utilization (PMCx043/PMCx044)**
```bash
# Memory vs cache bandwidth ratio
Memory_pressure = local_mem / (local_l2 + local_mem)

# Target: <10% for CPU-bound Rails workloads
```

### **Instruction Cache Efficiency (PMCx064)**
```bash
# Instruction cache hit rate for method dispatch
I_cache_hit_rate = (ic_fill_hit_s + ic_fill_hit_x) / 
                   (ic_fill_hit_s + ic_fill_hit_x + ic_fill_miss)

# Target: >95% for stable Rails applications
```

This reference provides the **exact AMD PMC register mappings** with official mnemonic format for precise Rails performance analysis on Zen 4/5 architectures! 🎯