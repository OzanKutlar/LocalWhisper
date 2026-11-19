; LocalWhisper AutoHotkey v1.1 Keystroke Sender
; Usage: AutoHotkeyU64.exe send_keys.ahk "<utf8_payload_file>" [Input|Event|Play]
; Uses interpreted AutoHotkey v1.1 Send, syntax so keys like {Enter}, {Tab}, ^a work.

#NoEnv
#SingleInstance Off
#NoTrayIcon
SetBatchLines, -1
SendMode, Input

if (A_Args.Length() < 1) {
    ExitApp, 1
}

filePath := A_Args[1]
if (!FileExist(filePath)) {
    ExitApp, 2
}

if (A_Args.Length() >= 2) {
    mode := A_Args[2]
    if (mode = "Event" || mode = "Input" || mode = "Play") {
        SendMode, %mode%
    }
}

FileRead, payload, *P65001 *t %filePath%
if (ErrorLevel) {
    ExitApp, 3
}

Send, %payload%
ExitApp, 0
