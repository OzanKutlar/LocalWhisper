; LocalWhisper AutoHotkey v1.1 Window Helper
; Usage: AutoHotkeyU64.exe window_action.ahk <focus|close> "<target_title_or_exe>"
; Exit codes:
;   0 = window matched and action performed
;   1 = invalid arguments
;   4 = no matching window found

#NoEnv
#SingleInstance Off
#NoTrayIcon
SetBatchLines, -1
SetTitleMatchMode, 2
DetectHiddenWindows, Off

if (A_Args.Length() < 2) {
    ExitApp, 1
}

action := A_Args[1]
target := A_Args[2]

if (target = "") {
    ExitApp, 1
}

hwnd := WinExist(target)
if (!hwnd) {
    hwnd := WinExist("ahk_exe " . target . ".exe")
}
if (!hwnd) {
    hwnd := WinExist("ahk_exe " . target)
}
if (!hwnd) {
    ExitApp, 4
}

if (action = "focus") {
    WinGet, minMax, MinMax, ahk_id %hwnd%
    if (minMax = -1) {
        WinRestore, ahk_id %hwnd%
    }
    WinActivate, ahk_id %hwnd%
    WinWaitActive, ahk_id %hwnd%, , 1
    ExitApp, 0
} else if (action = "close") {
    WinClose, ahk_id %hwnd%
    ExitApp, 0
}

ExitApp, 1
