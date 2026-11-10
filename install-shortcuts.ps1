# Puts a ytclip shortcut on the Desktop and in the Start Menu.
# Run once:  powershell -ExecutionPolicy Bypass -File install-shortcuts.ps1

$here = $PSScriptRoot
# pythonw is Python without the black console window.
$pythonw = (Get-Command pythonw -ErrorAction Stop).Source
$shell = New-Object -ComObject WScript.Shell

foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $link = $shell.CreateShortcut((Join-Path $folder 'ytclip.lnk'))
    $link.TargetPath = $pythonw
    $link.Arguments = '"' + (Join-Path $here 'app.py') + '"'
    $link.WorkingDirectory = $here
    $link.IconLocation = Join-Path $here 'web\icon.ico'
    $link.Description = 'Clip a YouTube video'
    $link.Save()
    Write-Host "Created $($link.FullName)"
}
