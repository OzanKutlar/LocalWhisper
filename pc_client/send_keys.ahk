; LocalWhisper AutoHotkey v1.1 Keystroke Sender
; Usage: AutoHotkeyU64.exe send_keys.ahk "<utf8_payload_file>" [Input|Event|Play] [delay_ms]
; The payload holds one pre-escaped Send unit per line (see ahk_payload.py).
; delay_ms > 0 types unit by unit with human-like jitter; 0 sends the single line at once.
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

typingDelay := 0
if (A_Args.Length() >= 3) {
    delayArg := A_Args[3]
    if delayArg is integer
        typingDelay := delayArg
}
if (typingDelay < 0 || typingDelay > 1000) {
    typingDelay := 0
}

FileRead, payload, *P65001 *t %filePath%
if (ErrorLevel) {
    ExitApp, 3
}

Loop, Parse, payload, `n
{
    if (A_LoopField = "")
        continue
    Send, %A_LoopField%
    if (typingDelay > 0)
        Sleep, % HumanDelay(A_LoopField, typingDelay)
}
ExitApp, 0

; Jittered pause after a unit: a bit longer after spaces, longer after punctuation and Enter.
HumanDelay(unit, base) {
    Random, jitter, 60, 140
    delay := base * jitter // 100
    if (unit = " ")
        delay += base * 4 // 5
    else if (unit = "." || unit = "," || unit = "{!}" || unit = "?" || unit = ";" || unit = ":" || unit = "{Enter}")
        delay += base * 3
    return delay
}
