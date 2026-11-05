#include <iostream>
#include <windows.h>
#include <string>

int main() {
    std::cout << "--- C++ Acoustic Trigger Backend ---" << std::endl;
    std::cout << "Creating Named Pipe... Waiting for Python daemon..." << std::endl;

    // 1. Create a Named Pipe in Windows
    HANDLE hPipe = CreateNamedPipeA(
        "\\\\.\\pipe\\AcousticTriggerPipe", // Pipe name
        PIPE_ACCESS_INBOUND,                // C++ will only read from this pipe
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        1,                                  // Max instances
        1024, 1024, 0, NULL);

    if (hPipe == INVALID_HANDLE_VALUE) {
        std::cerr << "Failed to create pipe." << std::endl;
        return 1;
    }

    // 2. Wait for the Python script to connect
    if (ConnectNamedPipe(hPipe, NULL) != FALSE) {
        std::cout << "Python connected successfully! Listening for taps...\n" << std::endl;
        
        char buffer[128];
        DWORD bytesRead;

        // 3. Continuous loop to read triggers
        while (ReadFile(hPipe, buffer, sizeof(buffer) - 1, &bytesRead, NULL) != FALSE) {
            buffer[bytesRead] = '\0'; 
            std::string trigger(buffer);
            trigger.erase(trigger.find_last_not_of(" \n\r\t") + 1);

            if (trigger == "LEFT") {
                std::cout << "[C++ EXECUTE] Left Macro Triggered (Pressing 'M')!" << std::endl;
                
                // Simulate pressing the 'M' key (Virtual-Key code 0x4D)
                INPUT ip;
                ip.type = INPUT_KEYBOARD;
                ip.ki.wScan = 0;
                ip.ki.time = 0;
                ip.ki.dwExtraInfo = 0;
                
                ip.ki.wVk = 0x4D; 
                ip.ki.dwFlags = 0; // Key press
                SendInput(1, &ip, sizeof(INPUT));
                
                // Simulate releasing the 'M' key
                ip.ki.dwFlags = KEYEVENTF_KEYUP;
                SendInput(1, &ip, sizeof(INPUT));
            } 
            else if (trigger == "RIGHT") {
                std::cout << "[C++ EXECUTE] Right Macro Triggered!" << std::endl;
                // You can add a different virtual key code here (e.g., 0x20 for Spacebar)
            }
        }
    }

    CloseHandle(hPipe);
    std::cout << "Connection closed." << std::endl;
    return 0;
}