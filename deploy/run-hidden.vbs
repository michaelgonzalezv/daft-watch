' Launch one rentals cycle with no visible console window.
' Task Scheduler runs:  wscript.exe "D:\Github\daft-watch\deploy\run-hidden.vbs"
' The third Run arg is True so this script blocks until the cycle finishes —
' that keeps the scheduled task "Running" for the whole cycle, so its
' MultipleInstancesPolicy (IgnoreNew) actually prevents overlapping runs.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c ""D:\Github\daft-watch\deploy\run-rentals.bat""", 0, True
