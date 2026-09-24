' 节点1 数据源实时看门狗 · 开机自启（用户级 Startup，无需管理员）
' 用 pythonw 无窗口启动 watchdog.py；watchdog 单实例锁保证不重复拉起
' 通用版：技能目录 = 本脚本所在目录；pythonw 动态检测（PATH 优先，python.exe 同目录换算回退）
' 部署：Startup 中放【快捷方式】指向本脚本（勿复制副本，否则脚本所在目录不再是技能目录）
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

skillDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = skillDir & "\watchdog.py"

Function FindPythonw()
    Dim exec, out, arr, ln, py, pyw
    On Error Resume Next
    ' 0) 优先本技能目录 .venv 的系统环境 pythonw（彻底脱离 TRAE 自带解释器）
    Dim venvw
    venvw = skillDir & "\.venv\Scripts\pythonw.exe"
    If fso.FileExists(venvw) Then
        FindPythonw = venvw
        Exit Function
    End If
    ' 1) PATH 中的 pythonw.exe
    Set exec = WshShell.Exec("cmd /c where pythonw.exe")
    If Err.Number = 0 Then
        out = exec.StdOut.ReadAll()
        arr = Split(Replace(out, vbCr, ""), vbLf)
        For Each ln In arr
            ln = Trim(ln)
            If ln <> "" And fso.FileExists(ln) Then
                FindPythonw = ln
                Exit Function
            End If
        Next
    End If
    ' 2) 回退：python.exe 同目录 pythonw.exe
    Set exec = WshShell.Exec("cmd /c where python.exe")
    If Err.Number = 0 Then
        out = exec.StdOut.ReadAll()
        arr = Split(Replace(out, vbCr, ""), vbLf)
        For Each ln In arr
            ln = Trim(ln)
            If ln <> "" Then
                py = ln
                pyw = fso.GetParentFolderName(py) & "\pythonw.exe"
                If fso.FileExists(pyw) Then
                    FindPythonw = pyw
                    Exit Function
                End If
            End If
        Next
    End If
    On Error GoTo 0
    FindPythonw = ""
End Function

pythonw = FindPythonw()
If pythonw = "" Then
    WScript.Quit 1
End If

' 工作目录设为技能目录，确保相对路径（data/logs/aux-data）正确
WshShell.CurrentDirectory = skillDir
WshShell.Run """" & pythonw & """ """ & target & """", 0, False
