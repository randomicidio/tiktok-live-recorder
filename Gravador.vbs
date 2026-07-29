' Abre o gravador sem janela preta de console.
' De dois cliques neste arquivo (ou crie um atalho dele na area de trabalho).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

pasta = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = pasta

' pythonw.exe roda sem console; se nao existir, cai para o python normal.
pythonw = "pythonw.exe"
On Error Resume Next
sh.Run """" & pythonw & """ """ & pasta & "\app.py""", 1, False
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python """ & pasta & "\app.py""", 1, False
End If
On Error GoTo 0
