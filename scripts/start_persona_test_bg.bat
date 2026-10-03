@echo off
rem 人格集对照实验常驻采样（disc-ctl / disc-exp 两臂）
rem
rem 为什么单独一个脚本：`persona-run` 不带 --group 会跑所有 enabled 组，
rem 而 config/persona_groups.yaml 里还有 6 个别组。这里用 --config 指向
rem 只含两臂的 persona_groups_test.yaml，**一个进程内组间串行**，
rem 不会把网关压垮。
rem
rem 注意：`start "" /b cmd /c "... 1>>file 2>>file"` 这层包装是必须的 ——
rem 子进程的 stdout/stderr 被重定向到文件，才不会继承父进程的管道句柄
rem 而把调用方挂住。
cd /d "%~dp0.."
set OMNIALPHA_ROOT=%CD%
set OPENAI_BASE_URL=http://69.12.85.185:7863/v1
call "%~dp0secrets.bat"
if not exist "%CD%\data\shared" mkdir "%CD%\data\shared"
start "" /b cmd /c ""%CD%\.venv\Scripts\pythonw.exe" -m omnialpha --root "%CD%" persona-run --config config/persona_groups_test.yaml --interval 300 1>>"%CD%\data\shared\persona-test.out" 2>>"%CD%\data\shared\persona-test.err""
echo started persona test loop (disc-ctl + disc-exp, interval 300s)
