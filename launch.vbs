Set WshShell = CreateObject("WScript.Shell")
WshShell.Run Chr(34) & CreateObject("Scripting.FileSystemObject").GetAbsolutePathName("run.bat") & Chr(34), 0, False
