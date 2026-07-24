' Arranca o Claude Pulse sem consola / starts Claude Pulse without a console
' Funciona a partir de qualquer pasta / works from any folder
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run "pythonw """ & dir & "\claude_pulse.py""", 0, False
