#include <iostream>
#include <windows.h>
#include <string>
#include <vector>
#include <sstream>
#include <cstring>

// Sends a single physical virtual-key + scan-code event (avoids VK_PACKET corruption)
void SendVKEvent(WORD vk, bool keyUp, HKL layout) {
    INPUT ip = {0};
    ip.type = INPUT_KEYBOARD;
    ip.ki.wVk = vk;
    ip.ki.wScan = static_cast<WORD>(MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC, layout));
    ip.ki.dwFlags = keyUp ? KEYEVENTF_KEYUP : 0;
    SendInput(1, &ip, sizeof(INPUT));
}

// Fallback for non-ASCII / Unicode characters not present on the active keyboard layout
void SendUnicodeChar(wchar_t c) {
    INPUT down = {0};
    down.type = INPUT_KEYBOARD;
    down.ki.wScan = c;
    down.ki.dwFlags = KEYEVENTF_UNICODE;
    SendInput(1, &down, sizeof(INPUT));
    Sleep(2);

    INPUT up = {0};
    up.type = INPUT_KEYBOARD;
    up.ki.wScan = c;
    up.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
    SendInput(1, &up, sizeof(INPUT));
    Sleep(8);
}

// Reads live Caps Lock toggle state synced with the foreground window
bool IsCapsLockOn() {
    HWND fg = GetForegroundWindow();
    DWORD fgThread = fg ? GetWindowThreadProcessId(fg, NULL) : 0;
    DWORD curThread = GetCurrentThreadId();
    bool attached = false;
    if (fgThread && fgThread != curThread) {
        attached = (AttachThreadInput(curThread, fgThread, TRUE) != FALSE);
    }
    bool caps = (GetKeyState(VK_CAPITAL) & 1) != 0;
    if (attached) {
        AttachThreadInput(curThread, fgThread, FALSE);
    }
    return caps;
}

// Unescapes \\n, \\t, and \\\\ sent over the pipe from Python
std::string UnescapeText(const std::string& input) {
    std::string out;
    out.reserve(input.size());
    for (size_t i = 0; i < input.size(); ++i) {
        if (input[i] == '\\' && i + 1 < input.size()) {
            char next = input[i + 1];
            if (next == 'n') { out.push_back('\n'); ++i; continue; }
            if (next == 't') { out.push_back('\t'); ++i; continue; }
            if (next == '\\') { out.push_back('\\'); ++i; continue; }
        }
        out.push_back(input[i]);
    }
    return out;
}

// Converts UTF-8 string to UTF-16 wstring
std::wstring Utf8ToWide(const std::string& str) {
    if (str.empty()) return std::wstring();
    int sizeNeeded = MultiByteToWideChar(CP_UTF8, 0, str.c_str(), (int)str.size(), NULL, 0);
    if (sizeNeeded <= 0) return std::wstring();
    std::wstring wstr(sizeNeeded, 0);
    MultiByteToWideChar(CP_UTF8, 0, str.c_str(), (int)str.size(), &wstr[0], sizeNeeded);
    return wstr;
}

bool SetClipboardUnicodeText(const std::wstring& text) {
    for (int attempt = 0; attempt < 5; ++attempt) {
        if (OpenClipboard(NULL)) {
            EmptyClipboard();
            size_t bytes = (text.size() + 1) * sizeof(wchar_t);
            HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE, bytes);
            if (!hMem) {
                CloseClipboard();
                return false;
            }
            void* ptr = GlobalLock(hMem);
            if (!ptr) {
                GlobalFree(hMem);
                CloseClipboard();
                return false;
            }
            std::memcpy(ptr, text.c_str(), bytes);
            GlobalUnlock(hMem);

            if (!SetClipboardData(CF_UNICODETEXT, hMem)) {
                GlobalFree(hMem);
                CloseClipboard();
                return false;
            }
            CloseClipboard();
            return true;
        }
        Sleep(10);
    }
    return false;
}

// Instant verbatim paste for multi-line or long code snippets (saves & restores clipboard text)
bool PasteViaClipboard(const std::wstring& wideText) {
    bool hadBackup = false;
    std::wstring backupText;

    if (IsClipboardFormatAvailable(CF_UNICODETEXT) && OpenClipboard(NULL)) {
        HANDLE hData = GetClipboardData(CF_UNICODETEXT);
        if (hData) {
            const wchar_t* locked = static_cast<const wchar_t*>(GlobalLock(hData));
            if (locked) {
                backupText = locked;
                hadBackup = true;
                GlobalUnlock(hData);
            }
        }
        CloseClipboard();
    }

    // Normalize \n to \r\n for native Windows editors
    std::wstring crlfText;
    crlfText.reserve(wideText.size() + 16);
    for (size_t i = 0; i < wideText.size(); ++i) {
        if (wideText[i] == L'\n' && (i == 0 || wideText[i - 1] != L'\r')) {
            crlfText.push_back(L'\r');
        }
        crlfText.push_back(wideText[i]);
    }

    if (!SetClipboardUnicodeText(crlfText)) {
        return false;
    }

    DWORD seqAfterSet = GetClipboardSequenceNumber();

    HWND fg = GetForegroundWindow();
    DWORD fgThread = fg ? GetWindowThreadProcessId(fg, NULL) : 0;
    HKL layout = GetKeyboardLayout(fgThread);

    SendVKEvent(VK_CONTROL, false, layout);
    Sleep(5);
    SendVKEvent('V', false, layout);
    Sleep(5);
    SendVKEvent('V', true, layout);
    Sleep(5);
    SendVKEvent(VK_CONTROL, true, layout);

    // Wait briefly so the target app finishes reading the clipboard before restoring
    Sleep(120);
    if (hadBackup && GetClipboardSequenceNumber() == seqAfterSet) {
        SetClipboardUnicodeText(backupText);
    }
    return true;
}

// Types short strings using real physical virtual keys (no VK_PACKET gibberish)
void TypeWithVirtualKeys(const std::wstring& wideText) {
    HWND fg = GetForegroundWindow();
    DWORD fgThread = fg ? GetWindowThreadProcessId(fg, NULL) : 0;
    HKL layout = GetKeyboardLayout(fgThread);
    bool capsOn = IsCapsLockOn();

    for (wchar_t c : wideText) {
        if (c == L'\n') {
            SendVKEvent(VK_RETURN, false, layout);
            Sleep(2);
            SendVKEvent(VK_RETURN, true, layout);
            Sleep(10);
            continue;
        }
        if (c == L'\t') {
            SendVKEvent(VK_TAB, false, layout);
            Sleep(2);
            SendVKEvent(VK_TAB, true, layout);
            Sleep(6);
            continue;
        }

        SHORT vkResult = VkKeyScanExW(c, layout);
        if (vkResult == -1) {
            SendUnicodeChar(c);
            continue;
        }

        WORD vk = LOBYTE(vkResult);
        BYTE shiftState = HIBYTE(vkResult);
        bool needShift = (shiftState & 1) != 0;
        bool needCtrl  = (shiftState & 2) != 0;
        bool needAlt   = (shiftState & 4) != 0;

        if (vk >= 'A' && vk <= 'Z' && capsOn) {
            needShift = !needShift;
        }

        if (needCtrl)  { SendVKEvent(VK_CONTROL, false, layout); Sleep(1); }
        if (needAlt)   { SendVKEvent(VK_MENU, false, layout);    Sleep(1); }
        if (needShift) { SendVKEvent(VK_SHIFT, false, layout);   Sleep(1); }

        SendVKEvent(vk, false, layout);
        Sleep(2);
        SendVKEvent(vk, true, layout);
        Sleep(2);

        if (needShift) { SendVKEvent(VK_SHIFT, true, layout);   Sleep(1); }
        if (needAlt)   { SendVKEvent(VK_MENU, true, layout);    Sleep(1); }
        if (needCtrl)  { SendVKEvent(VK_CONTROL, true, layout); Sleep(1); }

        Sleep(6);
    }
}

void TypeString(const std::string& escapedText) {
    std::string decoded = UnescapeText(escapedText);
    std::wstring wideText = Utf8ToWide(decoded);
    if (wideText.empty()) return;

    bool isMultiLineOrLong =
        (wideText.size() > 40) ||
        (wideText.find(L'\n') != std::wstring::npos) ||
        (wideText.find(L'\t') != std::wstring::npos);

    if (isMultiLineOrLong) {
        if (PasteViaClipboard(wideText)) {
            return;
        }
    }
    TypeWithVirtualKeys(wideText);
}

// Simulates pressing multiple keys simultaneously (e.g., Ctrl + Shift + C)
void PressCombo(const std::vector<int>& vkCodes) {
    std::vector<INPUT> inputs;

    for (int vk : vkCodes) {
        INPUT ip = {0};
        ip.type = INPUT_KEYBOARD;
        ip.ki.wVk = static_cast<WORD>(vk);
        inputs.push_back(ip);
    }

    for (auto it = vkCodes.rbegin(); it != vkCodes.rend(); ++it) {
        INPUT ip = {0};
        ip.type = INPUT_KEYBOARD;
        ip.ki.wVk = static_cast<WORD>(*it);
        ip.ki.dwFlags = KEYEVENTF_KEYUP;
        inputs.push_back(ip);
    }

    if (!inputs.empty()) {
        SendInput(static_cast<UINT>(inputs.size()), inputs.data(), sizeof(INPUT));
    }
}

int main() {
    const DWORD BUF_SIZE = 16384;
    HANDLE hPipe = CreateNamedPipeA(
        "\\\\.\\pipe\\AcousticTriggerPipe",
        PIPE_ACCESS_INBOUND,
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        1, BUF_SIZE, BUF_SIZE, 0, NULL);

    if (ConnectNamedPipe(hPipe, NULL) != FALSE || GetLastError() == ERROR_PIPE_CONNECTED) {
        std::vector<char> buffer(BUF_SIZE);
        DWORD bytesRead;

        while (ReadFile(hPipe, buffer.data(), BUF_SIZE - 1, &bytesRead, NULL) != FALSE) {
            buffer[bytesRead] = '\0';
            std::string trigger(buffer.data());

            // Strip only trailing CR/LF so intentional spaces at the end are preserved
            size_t endPos = trigger.find_last_not_of("\r\n");
            if (endPos == std::string::npos) continue;
            trigger.erase(endPos + 1);

            size_t colonPos = trigger.find(':');
            if (colonPos != std::string::npos) {
                std::string command = trigger.substr(colonPos + 1);

                if (command.rfind("TEXT:", 0) == 0) {
                    TypeString(command.substr(5));
                }
                else if (command.rfind("COMBO:", 0) == 0) {
                    std::string vkList = command.substr(6);
                    std::stringstream ss(vkList);
                    std::string item;
                    std::vector<int> vkCodes;

                    while (std::getline(ss, item, ',')) {
                        if (!item.empty()) vkCodes.push_back(std::stoi(item));
                    }
                    PressCombo(vkCodes);
                }
            }
        }
    }
    CloseHandle(hPipe);
    return 0;
}