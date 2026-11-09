#include <iostream>
#include <windows.h>
#include <string>
#include <vector>
#include <sstream>

// Simulates typing a word
void TypeString(const std::string& text) {
    for (char c : text) {
        INPUT inputs[2] = {0};
        inputs[0].type = INPUT_KEYBOARD;
        inputs[0].ki.wScan = c;
        inputs[0].ki.dwFlags = KEYEVENTF_UNICODE; 
        
        inputs[1].type = INPUT_KEYBOARD;
        inputs[1].ki.wScan = c;
        inputs[1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;

        SendInput(2, inputs, sizeof(INPUT));
        Sleep(5); 
    }
}

// Simulates pressing multiple keys simultaneously (e.g., Ctrl + Shift + C)
void PressCombo(const std::vector<int>& vkCodes) {
    std::vector<INPUT> inputs;
    
    // 1. Press all keys DOWN
    for (int vk : vkCodes) {
        INPUT ip = {0};
        ip.type = INPUT_KEYBOARD;
        ip.ki.wVk = vk;
        inputs.push_back(ip);
    }
    
    // 2. Release all keys UP (in reverse order)
    for (auto it = vkCodes.rbegin(); it != vkCodes.rend(); ++it) {
        INPUT ip = {0};
        ip.type = INPUT_KEYBOARD;
        ip.ki.wVk = *it;
        ip.ki.dwFlags = KEYEVENTF_KEYUP;
        inputs.push_back(ip);
    }
    
    if (!inputs.empty()) {
        SendInput(inputs.size(), inputs.data(), sizeof(INPUT));
    }
}

int main() {
    HANDLE hPipe = CreateNamedPipeA(
        "\\\\.\\pipe\\AcousticTriggerPipe", 
        PIPE_ACCESS_INBOUND,                
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        1, 1024, 1024, 0, NULL);

    if (ConnectNamedPipe(hPipe, NULL) != FALSE) {
        char buffer[256]; 
        DWORD bytesRead;

        while (ReadFile(hPipe, buffer, sizeof(buffer) - 1, &bytesRead, NULL) != FALSE) {
            buffer[bytesRead] = '\0'; 
            std::string trigger(buffer);
            trigger.erase(trigger.find_last_not_of(" \n\r\t") + 1);

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
                    
                    // Parse the comma-separated key codes from Python
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