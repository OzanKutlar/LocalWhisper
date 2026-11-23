; LocalWhisper AutoHotkey v1.1 Mouse Helper
; Usage:
;   AutoHotkeyU64.exe mouse_action.ahk click <x> <y> [Left|Right] [1|2]
;   AutoHotkeyU64.exe mouse_action.ahk scroll <x> <y> [up|down] [notches 1-15]
; Coordinates are physical screen pixels (the Python client and OCR script are DPI aware too).
; Exit codes: 0 = done, 1 = invalid arguments

#NoEnv
#SingleInstance Off
#NoTrayIcon
SetBatchLines, -1
CoordMode, Mouse, Screen
SetMouseDelay, 10

; Per-monitor DPI awareness (Windows 10 1607+). On older systems DllCall only sets ErrorLevel.
DllCall("SetThreadDpiAwarenessContext", "Ptr", -4, "Ptr")

if (A_Args.Length() < 3) {
    ExitApp, 1
}

action := A_Args[1]
x := A_Args[2]
y := A_Args[3]
if x is not integer
    ExitApp, 1
if y is not integer
    ExitApp, 1

if (action = "click") {
    button := (A_Args.Length() >= 4) ? A_Args[4] : "Left"
    if (button != "Left" && button != "Right")
        button := "Left"
    count := (A_Args.Length() >= 5) ? A_Args[5] : 1
    if count is not integer
        count := 1
    if (count < 1 || count > 2)
        count := 1
    MouseMove, %x%, %y%, 0
    Sleep, 40
    Click, %x% %y% %button% %count%
    ExitApp, 0
}

if (action = "scroll") {
    direction := (A_Args.Length() >= 4) ? A_Args[4] : "down"
    amount := (A_Args.Length() >= 5) ? A_Args[5] : 3
    if amount is not integer
        amount := 3
    if (amount < 1)
        amount := 1
    if (amount > 15)
        amount := 15
    wheel := (direction = "up") ? "WheelUp" : "WheelDown"
    MouseMove, %x%, %y%, 0
    Sleep, 30
    Click, %wheel% %amount%
    ExitApp, 0
}

ExitApp, 1
