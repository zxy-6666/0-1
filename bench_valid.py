"""对比 代码版本 x 数据版本 下 optimizer 的有效轮次/总轮次（干净版，直接传原始对象）"""
import sys, os, json, shutil, subprocess

ROOT = "/tmp/benchroot3"
os.makedirs(ROOT, exist_ok=True)
PY = "/root/.pyenv/versions/3.12.13/bin/python"
V11 = "/tmp/v11/半导体高优先级批次自动排程"
V10 = "/tmp/v10"
WORK = "/workspace/半导体高优先级批次自动排程"

# 通用 runner：CODE = 代码目录(含data), DATA 从代码目录读取
BOILER = '''
import sys, os
sys.path.insert(0, "@CODE@")
os.chdir("@CODE@")
import data_loader
base = os.getcwd()
def f(n): return os.path.join(base, "data", n) if os.path.isdir(os.path.join(base,"data")) else os.path.join(base, n)
def ex(n): return os.path.exists(f(n))
lots = data_loader.load_lot_list(f("lot_list.csv"), constraints_filepath=f("lot_constraints.csv"))
flows = data_loader.load_flow(f("flow.csv"))
fm = data_loader.get_product_flow_map(flows)
allf = [s for fl in fm.values() for s in fl]
qtimes = data_loader.load_qtime(f("qtime.csv"))
cts = data_loader.auto_repair_step_ct(flows, data_loader.load_step_ct(f("step_ct.csv")), None)
ct = data_loader.build_ct_lookup(cts)
scfg = data_loader.load_shift_config(f("shift_config.csv"))
st = sorted(tuple(map(int,x.start_time_str.split(":"))) for x in scfg if getattr(x,"start_time_str",None))
kw = {
  "ftf_qty_change": data_loader.load_ftf_qty_change(f("ftf_qty_change.csv")) if ex("ftf_qty_change.csv") else {},
  "special_lot_step_lookup": data_loader.load_special_lot_step(f("special_lot_step.csv")) if ex("special_lot_step.csv") else {},
  "priority_wait_map": data_loader.load_priority_wait(f("priority_wait.csv")) if ex("priority_wait.csv") else {},
  "eqp_constraints": data_loader.load_eqp_constraints(f("eqp_constraint.csv")) if ex("eqp_constraint.csv") else [],
  "step_time_window_constraints": data_loader.load_step_time_windows(f("step_time_window.csv")) if ex("step_time_window.csv") else [],
  "shift_change_times": data_loader.load_shift_change_times(f("shift_change_time.csv")) if ex("shift_change_time.csv") else [],
  "manual_adjusts": data_loader.load_manual_adjusts(f("manual_adjust.csv")) if ex("manual_adjust.csv") else [],
  "special_eqp_map": data_loader.load_special_eqp(f("special_eqp.csv")) if ex("special_eqp.csv") else {},
  "lot_constraints": data_loader.load_lot_constraints(f("lot_constraints.csv")) if ex("lot_constraints.csv") else [],
}
from optimizer import schedule_optimized
import json
res=dict()
for seed in [0,1,2]:
    le,ee,qa,meta = schedule_optimized(lots, allf, ct, qtimes, st,
        max_iterations=40, seed=seed, **kw)
    res[seed]={"valid":meta.get("valid_iterations"),"total":meta.get("total_iterations"),
               "score":meta.get("best_score"),"warning":meta.get("warning"),"entries":len(le)}
print("JSONOUT:"+json.dumps(res))
'''

def run(name, code_dir):
    d = os.path.join(ROOT, name)
    shutil.rmtree(d, ignore_errors=True)
    # 复制代码目录为运行环境（含它自己的 data）
    shutil.copytree(code_dir, d)
    script = BOILER.replace("@CODE@", d)
    p = os.path.join(ROOT, "_r.py")
    open(p, "w", encoding="utf-8").write(script)
    r = subprocess.run([PY, p], capture_output=True, text=True, cwd=ROOT, timeout=900)
    tail = r.stdout.split("JSONOUT:")[-1].strip()
    try:
        return json.loads(tail)
    except Exception:
        return {"error": tail[:200], "stderr": r.stderr[-500:]}

results = {}
results["v11 = v1.1代码+双设备数据"] = run("v11", V11)
results["v10 = v1.0原始代码+单设备数据"] = run("v10", V10)
# 我的修复代码 + v1.0 数据（当前工作区）
results["我的修复代码 + v1.0单设备数据"] = run("fix", WORK)

# 我的修复代码 + v1.1 双设备数据
fix11 = os.path.join(ROOT, "fix11_data")
shutil.rmtree(fix11, ignore_errors=True)
shutil.copytree(WORK, fix11)
shutil.copy(os.path.join(V11, "data", "flow.csv"), os.path.join(fix11, "data", "flow.csv"))
results["我的修复代码 + v1.1双设备数据"] = run("fix11", fix11)

for k, v in results.items():
    print("=" * 60)
    print(k)
    print(json.dumps(v, ensure_ascii=False, indent=1))