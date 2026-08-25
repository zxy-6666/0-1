"""根据 Stage/原Step 数据生成 flow.csv 和 step_ct.csv

用法（在项目根目录 /workspace 下执行）：
    python3 scripts/generate_flow_ct.py

说明：
  - 本脚本从下方硬编码的 "Stage/原Step" 原始数据生成系统使用的 data/flow.csv 与
    data/step_ct.csv。每步元组为 (step_name, ct_13pcs, ct_8pcs, ct_3pcs, eqp_ids)，
    CT 单位为小时，eqp_ids 为逗号分隔字符串（可为空）。
  - 2026-08-26 从当前 data/flow.csv + data/step_ct.csv 重建：数值与现有文件完全一致，
    锚点值（qty 3/8/13）保持 int/float 原样写法，其余片数由 3/8/13 锚点线性插值得到。
  - 注意：新增产品的原始数据导入/上传请使用 flow_importer.py 或 Web 界面「Flow 导入」，
    本脚本仅用于一次性生成或作为数据格式参考。
"""

MA_STAGES = {
    'AB1-IQC': [
        ('A005-R1-AB1IQC-WFS', 0.2, 0.2, 0.2, ''),
        ('A005-R1-AB1IQC-T7CODE', 0.06, 0.04, 0.02, 'PSWST002'),
        ('A005-R1-AB1IQC-INCOMING-MAP', 0.2, 0.2, 0.2, ''),
        ('A005-R1-AB1IQC-INSP', 0.2, 0.13, 0.06, 'PMAOM004'),
        ('A005-R1-AB1IQC-INSP-REV', 0.15, 0.15, 0.15, ''),
    ],
    'AB1-FC': [
        ('A005-R1-FC-REFLOW', 8.47, 5.57, 2.67, 'PKCON001,PKCON002,PKCON003'),
        ('A005-R1-FC-DEFLUX', 5.19, 3.43, 1.51, 'PKFCV001'),
        ('A005-R1-FC-SOH-MEAS', 27.17, 17.58, 7.99, 'PMSOH001,PMSOH003'),
        ('A005-R1-FC-GAP-MEAS', 2.79, 1.72, 0.64, 'PMSOH001,PMSOH003'),
        ('A005-R1-FC-IR-INSP', 3.25, 2, 0.75, 'PMSOH003'),
        ('A005-R1-FC-IR-INSP-REV', 3.25, 2, 0.75, ''),
        ('A005-R1-FC-INSP', 0.3, 0.19, 0.09, 'PMAOM004'),
        ('A005-R1-FC-INSP-REV', 0.15, 0.15, 0.15, ''),
    ],
    'AB1-UF': [
        ('A005-R1-UF-BAKE', 2.25, 2.16, 2.07, 'PLAOV002,PLAOV003'),
        ('A005-R1-UF-AUTO-SPLIT', 0.2, 0.13, 0.06, 'PSWST003'),
        ('A005-R1-UF-PLASMA', 0.12, 0.12, 0.12, 'PEDES101,PEDES102'),
        ('A005-R1-UF-DISPENSE', 3.85, 3.85, 1.56, 'PKUFD001,PKUFD002'),
        ('A005-R1-UF-CURE', 5.64, 5.63, 5.63, 'PKPOV001'),
        ('A005-R1-UF-INSP', 0.31, 0.2, 0.09, 'PMAOM004'),
        ('A005-R1-UF-INSP-REV', 0.61, 0.39, 0.17, ''),
        ('A005-R1-UF-CREEP-DISTANCE-MEAS', 10.56, 6.5, 2.44, 'PFTST001'),
    ],
    'AB1-DAF': [
        ('A005-R1-DAF-DUMMY-DIE', 13, 8, 3, 'PKDBD001'),
        ('A005-R1-DAF-CURE', 3.9, 3.71, 3.52, 'PKPOV002'),
        ('A005-R1-DAF-INSP', 0.3, 0.2, 0.09, 'PMAOM004'),
        ('A005-R1-DAF-INSP-REV', 0.19, 0.16, 0.12, ''),
        ('A005-R1-DAF-SAT', 39, 24, 9, 'PKSAT002,PKSAT006'),
        ('A005-R1-DAF-SAT-REV', 0.17, 0.14, 0.11, ''),
        ('A005-R1-DAF-BAKE', 2.24, 2.16, 2.07, 'PLAOV002,PLAOV003'),
        ('A005-R1-DAF-CHIPPING-INSP', 0.31, 0.2, 0.09, 'PMAOM004'),
        ('A005-R1-DAF-CHIPPING-INSP-REV', 0.52, 0.34, 0.16, ''),
        ('A005-R1-DAF-WARP-MEAS', 1.08, 0.68, 0.27, 'PMPFM001'),
    ],
    'AB1-MOD': [
        ('A005-R1-MD-PLASMA', 0.05, 0.05, 0.05, 'PEDES101,PEDES102'),
        ('A005-R1-MD-MOLDING', 4.4, 3.22, 2.04, 'PKMOD001'),
        ('A005-R1-MD-SHIFT-MEAS', 1.63, 1.02, 0.41, 'PMQEM001'),
        ('A005-R1-MD-INSP', 0.2, 0.13, 0.06, 'PMAOM004'),
        ('A005-R1-MD-INSP-REV', 0.06, 0.06, 0.06, ''),
        ('A005-R1-MD-TTV-MEAS', 1.97, 1.23, 0.48, 'PMPFM001'),
        ('A005-R1-MD-SAT', 26.22, 16.17, 6.12, 'PKSAT005'),
        ('A005-R1-MD-SAT-REV', 1.5, 0.92, 0.35, ''),
        ('A005-R1-MD-BAKE', 2.24, 2.16, 2.07, 'PLAOV002,PLAOV003'),
        ('A005-R1-MD-WARP-MEAS', 0.99, 0.62, 0.25, 'PMPFM001'),
    ],
    'AB1-BG2': [
        ('A005-R1-BG2-PRE-INSP', 0.2, 0.13, 0.06, 'PMAOM004'),
        ('A005-R1-BG2-PRE-INSP-REV', 4.67, 2.88, 1.08, ''),
        ('A005-R1-BG2-SORT', 0.2, 0.13, 0.06, 'PSWST003'),
        ('A005-R1-BG2-GRINDING', 2.56, 1.64, 0.73, 'PGGLI001'),
        ('A005-R1-BG2-SORT-02', 0.22, 0.14, 0.07, 'PSWST003'),
        ('A005-R1-BG2-GRINDING-02', 2.5, 1.63, 0.76, 'PGGLI003'),
        ('A005-R1-BG2-INSP1', 0.19, 0.12, 0.06, 'PMAOM004'),
        ('A005-R1-BG2-INSP1-REV', 0.11, 0.09, 0.08, ''),
        ('A005-R1-BG2-THK-MEAS', 0.66, 0.44, 0.21, 'PMPFM001'),
        ('A005-R1-BG2-WARP-MEAS', 0.58, 0.37, 0.17, 'PMPFM001'),
        ('A005-R1-BG2-TOPTHK-MEAS', 0.26, 0.21, 0.17, 'PMSOH001,PMSOH002'),
        ('A005-R1-BG2-GAP-MEAS', 0.6, 0.47, 0.34, 'PMSOH001,PMSOH002'),
        ('A005-R1-BG2-UNIT-MARKING', 0.75, 0.47, 0.19, 'PBUMK001'),
        ('A005-R1-BG2-DEPTH-SHIFT-MEAS', 3.01, 2.06, 1.11, 'PMWLI001'),
        ('A005-RA-BG2-2D-SCAN', 1.3, 0.8, 0.3, 'PMHOI005,PMTOI005'),
        ('A005-R1-BG2-DATA-REVIEW', 0.29, 0.29, 0.29, ''),
        ('A005-R1-BG2-INSP2', 0.3, 0.19, 0.08, 'PMAOM004'),
        ('A005-R1-BG2-INSP2-REV', 0.07, 0.07, 0.07, ''),
    ],
    'AB1-TRM2': [
        ('A005-R1-TRIM2-TRIMMING', 5.78, 3.59, 1.39, 'PGTRM101,PGTRM102'),
        ('A005-R1-TRIM2-CHIPPING-INSP', 0.12, 0.08, 0.04, 'PMAOM004'),
        ('A005-R1-TRIM2-CHIPPING-INSP-REV', 0.11, 0.11, 0.11, ''),
        ('A005-R1-TRIM2-WIDTH-DEPTH-MEAS1', 0.36, 0.23, 0.09, 'PMPFM001'),
        ('A005-R1-TRIM2-DIA-MEAS', 0.26, 0.2, 0.15, 'PMQEM001'),
        ('A005-R1-TRIM2-NOTCH-CUTTING', 1.11, 0.72, 0.33, 'PKNCT001'),
        ('A005-R1-TRIM2-WIDTH-DEPTH-MEAS2', 1.19, 0.74, 0.29, 'PMOMI003'),
        ('A005-R1-TRIM2-INSP', 0.1, 0.07, 0.04, 'PMAOM004'),
        ('A005-R1-TRIM2-INSP-REV', 0.07, 0.07, 0.07, ''),
    ],
    'BVR-DB2': [
        ('A005-R1-DB2-SORT', 0.2, 0.13, 0.06, 'PSWST001'),
        ('A005-R1-DB2-DEBOND', 2.18, 1.43, 0.68, 'RBDEB001'),
        ('A005-R1-DB2-DES', 0.31, 0.31, 0.31, 'PEDES103'),
        ('A005-R1-DB2-BUFFER-ETCH', 2.83, 1.74, 0.65, 'PWETC001'),
        ('A005-R1-DB2-INSP', 0.2, 0.13, 0.07, 'PMAOM008'),
        ('A005-R1-DB2-INSP-REV', 2.28, 1.41, 0.53, ''),
        ('A005-R1-DB2-CLEAN', 6.13, 3.83, 1.52, 'PBDEB001,PBDEB002'),
        ('A005-R1-DB2-OMINSP', 0.27, 0.18, 0.1, 'PMAOM001,PMAOM008'),
        ('A005-R1-DB2-OMINSP-REV', 0.19, 0.12, 0.05, ''),
        ('A005-R1-DB2-WPGMEAS', 0.8, 0.5, 0.2, 'PMPFM001,PMPFM002'),
        ('A005-R1-TRIM3-TRIMING', 2.19, 1.41, 0.64, 'PKNCT001'),
        ('A005-R1-TRIM3-WIDTH-DEPTH-MEAS', 1.2, 0.75, 0.3, 'PMOMI003'),
        ('A005-R1-TRIM3-CHIPPING-INSP', 0.18, 0.11, 0.05, 'PMAOM004'),
        ('A005-R1-TRIM3-CHIPPING-INSP-REV', 0.06, 0.06, 0.06, ''),
        ('A005-R1-DB2-POSTET3-SCRB', 1.35, 0.84, 0.32, 'PWSCR002'),
        ('A005-R1-TRIM3-EDGE-2D-SCAN', 1.3, 0.8, 0.3, 'PMLOI005'),
        ('A005-R1-DB2-EDGE-DATA-REVIEW', 0.14, 0.11, 0.08, ''),
        ('A005-R1-DB2-POST-AOI-SCRB', 1.27, 0.8, 0.33, 'PWSCR002'),
        ('A005-R1-DB2-AOI-SPLIT', 0.2, 0.13, 0.06, ''),
        ('A005-R1-TRIM3-2D-SCAN', 3.25, 2, 0.75, 'PMHOI005'),
        ('A005-R1-DB2-DATA-REVIEW', 1.81, 1.22, 0.63, ''),
        ('A005-R1-DB2-REPORT', 0, 0, 0, ''),
        ('A005-R1-DB2-MERGE-MAP-CHECK', 0, 0, 0, ''),
        ('A005-R1-DB2-MAP-DISPOSAL', 0, 0, 0, ''),
        ('A005-R1-DB2-TRAN', 0.2, 0.13, 0.06, 'PSWST003'),
    ],
}

P1_STAGES = {
    'AB1-FC': [
        ('A005-P1-FC-BANK', 0.02, 0.01, 0.01, ''),
        ('A005-P1-FC-DUMMY', 0.02, 0.01, 0.01, ''),
        ('A005-P1-FC-REFLOW', 8.47, 5.57, 2.67, 'PKCON001,PKCON002,PKCON003'),
        ('A005-P1-FC-DEFLUX', 5.19, 3.43, 1.51, 'PKFCV001'),
        ('A005-P1-FC-SOH-MEAS', 1.88, 1.37, 0.85, 'PMSOH001,PMSOH003'),
        ('A005-P1-FC-GAP-MEAS', 0.37, 0.31, 0.25, 'PMSOH001,PMSOH003'),
        ('A005-P1-FC-INSP', 0.3, 0.19, 0.09, 'PMAOM004'),
        ('A005-P1-FC-INSP-REV', 0.12, 0.12, 0.12, ''),
    ],
    'AB1-UF': [
        ('A005-P1-UF-BAKE', 2.26, 2.17, 2.08, 'PLAOV002,PLAOV003'),
        ('A005-P1-UF-AUTO-SPLIT', 0.09, 0.05, 0.02, 'PSWST003'),
        ('A005-P1-UF-PLASMA', 0.72, 0.45, 0.19, 'PEDES101,PEDES102'),
        ('A005-P1-UF-DISPENSE', 7.1, 3.85, 1.56, 'PKUFD001,PKUFD002'),
        ('A005-P1-UF-CURE', 6.09, 5.87, 5.65, 'PKPOV001'),
        ('A005-P1-UF-INSP', 0.29, 0.2, 0.1, 'PMAOM004'),
        ('A005-P1-UF-INSP-REV', 0.22, 0.17, 0.12, ''),
        ('A005-P1-UF-CREEP-DISTANCE-MEAS', 6.55, 4.11, 1.66, 'PFTST001'),
    ],
    'AB1-DAF': [
        ('A005-P1-DAF-DUMMY-DIE', 2.2, 1.37, 0.54, 'PKDBD001'),
        ('A005-P1-DAF-CURE', 3.9, 3.72, 3.53, 'PKPOV002'),
        ('A005-P1-DAF-INSP', 0.3, 0.2, 0.1, 'PMAOM004'),
        ('A005-P1-DAF-INSP-REV', 0.33, 0.25, 0.16, ''),
        ('A005-P1-DAF-SAT', 39, 24, 9, 'PKSAT002,PKSAT006'),
        ('A005-P1-DAF-SAT-REV', 0.72, 0.45, 0.19, ''),
        ('A005-P1-DAF-BAKE', 2.25, 2.17, 2.08, 'PLAOV002,PLAOV003'),
        ('A005-P1-DAF-CHIPPING-INSP', 0.3, 0.19, 0.09, 'PMAOM004'),
        ('A005-P1-DAF-CHIPPING-INSP-REV', 0.26, 0.18, 0.1, ''),
        ('A005-P1-DAF-WARP-MEAS', 1.12, 0.71, 0.29, 'PMPFM001'),
    ],
    'AB1-MOD': [
        ('A005-P1-MD-PLASMA', 0.23, 0.15, 0.07, 'PEDES101,PEDES102'),
        ('A005-P1-MD-MOLDING', 4.4, 3.22, 2.04, 'PKMOD001'),
        ('A005-P1-MD-SHIFT-MEAS', 1.46, 0.91, 0.36, 'PMQEM001'),
        ('A005-P1-MD-INSP', 0.2, 0.13, 0.06, 'PMAOM004'),
        ('A005-P1-MD-INSP-REV', 0.13, 0.1, 0.08, ''),
        ('A005-P1-MD-TTV-MEAS', 2.05, 1.28, 0.51, 'PMPFM001'),
        ('A005-P1-MD-SAT', 26.27, 16.21, 6.14, 'PKSAT005'),
        ('A005-P1-MD-SAT-REV', 0.72, 0.45, 0.19, ''),
        ('A005-P1-MD-BAKE', 2.24, 2.16, 2.07, 'PLAOV002,PLAOV003'),
        ('A005-P1-MD-WARP-MEAS', 0.72, 0.46, 0.19, 'PMPFM001'),
    ],
    'AB1-BG2': [
        ('A005-P1-BG2-PRE-INSP', 0.22, 0.14, 0.07, 'PMAOM004'),
        ('A005-P1-BG2-PRE-INSP-REV', 0.2, 0.13, 0.06, ''),
        ('A005-P1-BG2-SORT', 0.23, 0.2, 0.17, 'PSWST003'),
        ('A005-P1-BG2-GRINDING', 2.34, 1.52, 0.7, 'PGGLI001'),
        ('A005-P1-BG2-SORT-02', 0.23, 0.15, 0.07, 'PSWST003'),
        ('A005-P1-BG2-GRINDING-02', 2.18, 1.45, 0.71, 'PGGLI003'),
        ('A005-P1-BG2-INSP1', 0.2, 0.13, 0.06, 'PMAOM004'),
        ('A005-P1-BG2-INSP1-REV', 0.13, 0.1, 0.07, ''),
        ('A005-P1-BG2-THK-MEAS', 0.93, 0.59, 0.24, 'PMPFM001'),
        ('A005-P1-BG2-WARP-MEAS', 0.83, 0.52, 0.21, 'PMPFM001'),
        ('A005-P1-BG2-TOPTHK-MEAS', 0.66, 0.43, 0.19, 'PMSOH001,PMSOH002,PMSOH003'),
        ('A005-P1-BG2-GAP-MEAS', 1.5, 0.96, 0.42, 'PMSOH001,PMSOH002,PMSOH003'),
        ('A005-P1-BG2-UNIT-MARKING', 1.11, 0.7, 0.28, 'PBUMK001'),
        ('A005-P1-BG2-DEPTH-SHIFT-MEAS', 1.62, 1.34, 1.06, 'PMWLI001'),
        ('A005-P1-BG2-2D-SCAN', 1.74, 1.07, 0.4, 'PMHOI005,PMTOI005'),
        ('A005-P1-BG2-DATA-REVIEW1', 2.1, 1.37, 0.63, ''),
        ('A005-P1-BG2-INSP2', 0.31, 0.2, 0.09, 'PMAOM004'),
        ('A005-P1-BG2-INSP2-REV', 0.14, 0.11, 0.09, ''),
    ],
    'AB1-TRM2': [
        ('A005-P1-TRIM2-TRIMMING', 5.82, 3.6, 1.38, 'PGTRM101,PGTRM102'),
        ('A005-P1-TRIM2-CHIPPING-INSP', 0.19, 0.12, 0.06, 'PMAOM004'),
        ('A005-P1-TRIM2-CHIPPING-INSP-REV', 0.09, 0.09, 0.08, ''),
        ('A005-P1-TRIM2-WIDTH-DEPTH-MEAS1', 0.26, 0.18, 0.09, 'PMPFM001'),
        ('A005-P1-TRIM2-DIA-MEAS', 0.3, 0.25, 0.19, 'PMQEM001'),
        ('A005-P1-TRIM2-NOTCH-CUTTING', 1.08, 0.71, 0.33, 'PKNCT001'),
        ('A005-P1-TRIM2-WIDTH-DEPTH-MEAS2', 0.35, 0.27, 0.19, 'PMOMI003'),
        ('A005-P1-TRIM2-INSP', 0.18, 0.12, 0.05, 'PMAOM004'),
        ('A005-P1-TRIM2-INSP-REV', 0.07, 0.07, 0.07, ''),
    ],
    'BVR-DB2': [
        ('A005-P1-DB2-SORT', 0.21, 0.14, 0.06, 'PSWST001'),
        ('A005-P1-DB2-DEBONDING', 2.79, 1.82, 0.85, 'RBDEB001'),
        ('A005-P1-DB2-DES', 1.11, 0.74, 0.38, 'PEDES103'),
        ('A005-P1-DB2-BUFFER-ETCH', 3.23, 1.99, 0.75, 'PWETC001'),
        ('A005-P1-DB2-BUFFER-ETCH-INSP', 0.26, 0.17, 0.08, 'PMAOM008'),
        ('A005-P1-DB2-BUFFER-ETCH-INSP-REV', 0.53, 0.35, 0.16, ''),
        ('A005-P1-DB2-CLEAN', 6.42, 4.05, 1.67, 'PBDEB001,PBDEB002'),
        ('A005-P1-DB2-OMINSP', 0.32, 0.21, 0.11, 'PMAOM001,PMAOM008'),
        ('A005-P1-DB2-OMINSP-REV', 1.22, 0.75, 0.28, ''),
        ('A005-P1-DB2-WPGMEAS', 0.75, 0.48, 0.2, 'PMPFM001,PMPFM002'),
        ('A005-P1-TRIM3-TRIMING', 2.28, 1.46, 0.65, 'PKNCT001'),
        ('A005-P1-TRIM3-WIDTH-DEPTH-MEAS', 1.22, 0.78, 0.33, 'PMOMI003'),
        ('A005-P1-TRIM3-CHIPPING-INSP', 0.15, 0.1, 0.04, 'PMAOM004'),
        ('A005-P1-TRIM3-CHIPPING-INSP-REV', 0.34, 0.22, 0.11, ''),
        ('A005-P1-DB2-SCRB', 1.29, 0.8, 0.31, 'PWSCR002'),
        ('A005-P1-TRIM3-EDGE-2D-SCAN', 0.45, 0.28, 0.12, 'PMLOI005'),
        ('A005-P1-TRIM3-EDGE-DATA-REVIEW', 0.24, 0.23, 0.23, ''),
        ('A005-P1-DB2-POST-AOI-SCRB', 1.27, 0.8, 0.33, 'PWSCR002'),
        ('A005-P1-TRIM3-2D-SCAN', 2.47, 1.55, 0.64, 'PMHOI005'),
        ('A005-P1-TRIM3-DATA-REVIEW', 1.03, 0.89, 0.74, ''),
        ('AB1-INKMAP-COLLECT', 0.0, 0.0, 0.0, ''),
        ('A005-P1-OQC1-REPORT', 0.5, 0.5, 0.5, ''),
        ('A005-P1-DB2-TRAN', 0.5, 0.5, 0.5, 'PSWST003'),
        ('A005-P1-DB2-BANK', 0.5, 0.5, 0.5, ''),
    ],
    'AB1-PKS': [
        ('A005-P1-PKS-MOUNT', 0.59, 0.39, 0.2, 'PKFRL001'),
        ('A005-P1-PKS-MOUNT-INSP', 0.48, 0.32, 0.16, ''),
        ('A005-P1-PKS-MOUNT-INSP-REV', 0.49, 0.32, 0.15, ''),
        ('A005-P1-PKS-SEMI-SAW', 6.03, 3.76, 1.49, 'PGTRM005'),
        ('A005-P1-PKS-Z1-MEAS', 2.6, 1.6, 0.6, 'PMWLI001'),
        ('A005-P1-PKS-LG', 3.37, 2.16, 0.95, 'PGLGV004'),
        ('A005-P1-PKS-LG-MEAS-01', 3.37, 2.16, 0.95, 'PMWLI002'),
        ('A005-P1-PKS-LG-MEAS', 3.25, 2, 0.75, 'PMWLI001'),
        ('A005-P1-PKS-DIE-SAW', 1.96, 1.24, 0.52, 'PGTRM003'),
        ('A005-P1-PKS-Z2-INSP-MEAS', 0.21, 0.19, 0.16, 'PMOMI001'),
        ('A005-P1-PKS-Z2-MEAS-REV', 0.29, 0.25, 0.21, ''),
        ('A005-P1-PKS-UV-ERASE', 0.2, 0.13, 0.06, 'PGUVE002'),
        ('A005-P1-PKS-SCRB2', 1.23, 0.76, 0.29, 'PKSCR002'),
    ],
    'AB1-OQC2': [
        ('A005-P1-OQC2-DS-2D-SCAN', 4.15, 2.57, 0.98, 'PMHOI015'),
        ('A005-P1-OQC2-DATA-REVIEW1', 7.5, 4.85, 2.19, ''),
        ('A005-P1-OQC2-REPORT', 0.0, 0.0, 0.0, ''),
        ('A005-P1-OQC2-BANK', 0.09, 0.08, 0.06, ''),
    ],
}


import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _interpolate_ct(ct_3, ct_8, ct_13):
    """按 3/8/13 片锚点分段线性插值，返回 1-13 片 CT（分钟）。

    - qty 1-2: 用 (3, ct_3)-(8, ct_8) 直线外推
    - qty 3-8: (3, ct_3)-(8, ct_8) 内插
    - qty 9-13: (8, ct_8)-(13, ct_13) 内插
    锚点缺失时：全部为 0 或按唯一锚点取常量。
    """
    anchors = []
    if ct_3 is not None:
        anchors.append((3, ct_3))
    if ct_8 is not None:
        anchors.append((8, ct_8))
    if ct_13 is not None:
        anchors.append((13, ct_13))
    if not anchors:
        return {qty: 0.0 for qty in range(1, 14)}
    if len(anchors) < 2:
        return {qty: anchors[0][1] for qty in range(1, 14)}
    result = {}
    for qty in range(1, 14):
        if qty <= anchors[0][0]:
            a, b = anchors[0], anchors[1]
            val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
        elif qty >= anchors[-1][0]:
            a, b = anchors[-2], anchors[-1]
            val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
        else:
            for i in range(len(anchors) - 1):
                if anchors[i][0] <= qty <= anchors[i + 1][0]:
                    a, b = anchors[i], anchors[i + 1]
                    val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
                    break
            else:
                val = anchors[0][1]
        result[qty] = round(val, 4)
    return result


def _anchor_str(ct_h):
    """锚点（qty 3/8/13）字符串：int 保持整数写法（如 180），float 保留 .0 写法（如 12.0）。"""
    if isinstance(ct_h, int):
        return str(ct_h * 60)
    return str(round(ct_h * 60, 4))


def generate(product_name, stages):
    """由某个产品的 Stage/原Step 数据生成 flow.csv 与 step_ct.csv 的行。"""
    flow_rows, ct_rows = [], []
    stage_num = 10
    for stage, steps in stages.items():
        for i, (step_name, c13, c8, c3, eqp) in enumerate(steps, 1):
            step_number = f"{stage_num}.{i:03d}"
            flow_rows.append((product_name, step_number, step_name, stage, eqp))
            anchors = {13: c13, 8: c8, 3: c3}
            cts = _interpolate_ct(float(c3) * 60, float(c8) * 60, float(c13) * 60)
            for qty in range(1, 14):
                val = _anchor_str(anchors[qty]) if qty in anchors else str(cts[qty])
                ct_rows.append((product_name, step_number, step_name, str(qty), val))
        stage_num += 10
    return flow_rows, ct_rows


def main():
    all_flow, all_ct = [], []
    for pname, stages in (("A005-MA", MA_STAGES), ("A005-P1", P1_STAGES)):
        f, c = generate(pname, stages)
        all_flow += f
        all_ct += c

    flow_path = os.path.join(DATA_DIR, "flow.csv")
    ct_path = os.path.join(DATA_DIR, "step_ct.csv")
    with open(flow_path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(["product_name", "step_number", "step_name", "stage_name", "eqp_id"]) + "\n")
        for row in all_flow:
            fh.write("\t".join(row) + "\n")
    with open(ct_path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(["product_name", "step_number", "step_name", "qty", "step_ct"]) + "\n")
        for row in all_ct:
            fh.write("\t".join(row) + "\n")
    print(f"已生成 {flow_path}（{len(all_flow)} 行）和 {ct_path}（{len(all_ct)} 行）")


if __name__ == "__main__":
    main()
