#include <iostream>
#include <windows.h>
#include <string>
#include <vector>
#include <sstream>

// Presses a single standard virtual key (e.g., VK_RETURN, VK_TAB)
void PressSingleVK(WORD vk) {
    INPUT inputs[2] = {0};
    inputs[0].type = INPUT_KEYBOARD;
    inputs[0].ki.wVk = vk;

    inputs[1].type = INPUT_KEYBOARD;
    inputs[1].ki.wVk = vk;
    inputs[1].ki.dwFlags = KEYEVENTF_KEYUP;

    SendInput(2, inputs, sizeof(INPUT));
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

// Converts UTF-8 string to UTF-16 wstring for Win32 Unicode injection
std::wstring Utf8ToWide(const std::string& str) {
    if (str.empty()) return std::wstring();
    int sizeNeeded = MultiByteToWideChar(CP_UTF8, 0, str.c_str(), (int)str.size(), NULL, 0);
    std::wstring wstr(sizeNeeded, 0);
    MultiByteToWideChar(CP_UTF8, 0, str.c_str(), (int)str.size(), &wstr[0], sizeNeeded);
    return wstr;
}

// Simulates typing a string or multi-line code snippet
void TypeString(const std::string& escapedText) {
    std::string decoded = UnescapeText(escapedText);
    std::wstring wideText = Utf8ToWide(decoded);

    for (wchar_t c : wideText) {
        if (c == L'\n') {
            PressSingleVK(VK_RETURN);
            Sleep(10);
            continue;
        }
        if (c == L'\t') {
            PressSingleVK(VK_TAB);
            Sleep(5);
            continue;
        }

        INPUT inputs[2] = {0};
        inputs[0].type = INPUT_KEYBOARD;
        inputs[0].ki.wScan = c;
        inputs[0].ki.dwFlags = KEYEVENTF_UNICODE;

        inputs[1].type = INPUT_KEYBOARD;
        inputs[1].ki.wScan = c;
        inputs[1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;

        SendInput(2, inputs, sizeof(INPUT));
        Sleep(2);
    }
}

// Simulates pressing multiple keys simultaneously (e.g., Ctrl + Shift + C)
void PressCombo(const std::vector<int>& vkCodes) {
    std::vector<INPUT> inputs;

    for (int vk : vkCodes) {
        INPUT ip = {0};
        ip.type = INPUT_KEYBOARD;
        ip.ki.wVk = vk;
        inputs.push_back(ip);
    }

    for (auto it = vkCodes.rbegin(); it != vkCodes.rend(); ++it) {
        INPUT ip = {0};
        ip.type = INPUT_KEYBOARD;
        ip.ki.wVk = *it;
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

            // Strip only trailing CR/LF so intentional trailing spaces/tabs in snippets stay intact
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