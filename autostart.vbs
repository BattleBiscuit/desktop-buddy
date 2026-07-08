' Launches Russgeist silently (no console window) using pythonw.exe.
' This file is meant to be referenced by a shortcut in the Startup folder,
' not moved there itself - see README/instructions for setup.
Set WshShell = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
WshShell.CurrentDirectory = scriptDir
WshShell.Run "pythonw.exe main.py", 0, False
