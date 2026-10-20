' Stop the background bot using data\bot.pid (hidden, no console window)
Option Explicit
Dim sh, fso, here, pidFile, pid
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
pidFile = here & "\data\bot.pid"
If Not fso.FileExists(pidFile) Then
  MsgBox "Bot is not running (data\bot.pid not found).", 64, "tw-line-faq-bot"
  WScript.Quit 0
End If
pid = Trim(fso.OpenTextFile(pidFile, 1).ReadAll)
sh.Run "taskkill /PID " & pid & " /T /F", 0, True
fso.DeleteFile pidFile
MsgBox "Stopped.", 64, "tw-line-faq-bot"
