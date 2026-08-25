排程调度系统 —— Windows 一键打包说明
========================================

★ 这份工具是给你【生成 .exe】用的，一次打包，之后生成的 exe
  可以直接发给同事，对方电脑【不用安装 Python】。

一、你需要什么
  一台 Windows 电脑（Win10 / Win11），能上网。第一次运行会自动
  下载约 10MB 的免安装 Python 和依赖，之后都在本机完成。

二、怎么打包（三步）
  1. 把整个文件夹放到任意位置（路径建议不含中文和空格，如 D:\scheduler）。
  2. 双击【一键打包.bat】（脚本为纯英文，避免中文乱码问题）。
  3. 等命令行窗口自动跑完，看到 "DONE!" 即完成。

三、打包完成后，在哪里拿成品
  本文件夹下会生成：
    publish\schedule_app_win\
        schedule_app.exe     ← 免安装程序（发给别人这个）
        data\                ← 默认配置表（放根目录）
        output\              ← 排程结果输出
        README.txt
        SOP_使用说明.md      ← 使用 SOP（各数据表用途与示例）
        SOP_使用说明.docx    ← 使用 SOP（Word 版，可直接编辑打印）
        文件分类说明.md      ← 文件分类说明（数据/源码/工具/缓存如何区分）
    schedule_app_windows.zip   ← 打包好的压缩包，直接分发

  把 "publish\schedule_app_win" 整个目录（或 zip 解压后的目录）
  发给同事，对方双击 schedule_app.exe，程序自动打开浏览器进入系统
  http://127.0.0.1:5000/ ，不需要安装任何东西。

四、数据都放在哪（重点）
  【exe 所在目录】就是根目录，数据在它旁边：
    data/    输入配置表：flow.csv、step_ct.csv、lot_list.csv、qtime.csv、
              shift_config.csv、special_eqp.csv、optimizer_config.json 等。
              可直接改 CSV，也可在网页里编辑自动写回。
    output/  每次排程结果：快照、导出的 Excel、甘特图 PNG。
  想换配置，就改 data/ 下的文件；把整个目录搬到别的电脑，数据跟着走。

五、如何退出程序（重要）
  这个程序是后台运行的（没有可见窗口），网页就是你看到的界面。退出方式：
  1. 在网页右上角点红色【退出程序】按钮 —— 程序立刻彻底关闭，最推荐。
  2. 直接关闭浏览器标签页 / 整个浏览器 —— 程序检测到浏览器关闭后，
     约 15 秒内会自动退出，不用去任务管理器清理。
  · 请勿只关浏览器后以为程序还在占着后台——现在它会自己退出；
    若 15 秒后任务管理器里还有 schedule_app.exe，多半是旧版本，重新打包即可。

六、常见问题
  · 端口被占用（5000 被别的程序占用）：
      设置环境变量 PORT=5099 后再运行 exe（Windows Cmd：set PORT=5099）。
  · 第一次双击 exe 启动较慢（单文件要解压到临时目录），属正常现象。
  · 打包中途下载很慢/失败：多半是网络问题，重试即可。
  · 生成的 exe 体积较大（约 60-90MB），因为内置了 Python 和全部依赖，属正常。
  · 若系统已有 Python 3.10-3.12，脚本仍使用内置免安装 Python 3.11，
    保证依赖版本一致、不污染系统环境。

七、备注
  · 脚本会自动：下载免安装 Python → 引导 pip → 装依赖 →
    PyInstaller 打 exe → 组装 publish 目录 → 生成 zip。
  · 手动打包命令可参考（在能联网的 Python 3.10-3.12 环境）：
      pip install flask pandas openpyxl matplotlib pyinstaller
      pyinstaller --onefile --windowed --name schedule_app --paths src ^
        --add-data "src\web\templates;templates" ^
        --hidden-import data_loader --hidden-import models --hidden-import scheduler ^
        --hidden-import optimizer --hidden-import optimizer_config --hidden-import validation ^
        --hidden-import visualization --hidden-import snapshot_store --hidden-import outputs ^
        --hidden-import health_check --hidden-import flow_importer --hidden-import paths ^
        src\web\app.py
    成品在 dist\schedule_app.exe。
