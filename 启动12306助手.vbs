Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
base = files.GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & base & "\dist\12306余票助手稳定版.exe"" --open-browser", 0, False
