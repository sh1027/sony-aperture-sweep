// UTF-8 line protocol on stdin; one JSON response per line on stdout.
// Sony headers/binaries are supplied by the user's SDK installation.
#include "CameraRemote_SDK.h"
#include "IDeviceCallback.h"
#include "protocol_numbers.h"
#include <Windows.h>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace sdk = SCRSDK;
namespace fs = std::filesystem;
using namespace std::chrono_literals;

std::string utf8(const wchar_t* value) {
    if (!value || !*value) return {};
    int size = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), size, nullptr, nullptr);
    result.pop_back();
    return result;
}

std::string quote(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char ch : value) {
        if (ch == '"' || ch == '\\') out << '\\' << ch;
        else if (ch < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(ch);
        else out << ch;
    }
    out << '"';
    return out.str();
}

void check(sdk::CrError error, const std::string& action) {
    if (error != sdk::CrError_None) {
        std::ostringstream text;
        text << action << " failed (Sony SDK 0x" << std::hex << error << ')';
        throw std::runtime_error(text.str());
    }
}

class Runtime {
public:
    sdk::ICrEnumCameraObjectInfo* cameras = nullptr;
    Runtime() {
        if (!sdk::Init()) throw std::runtime_error("Sony SDK initialization failed");
        try { check(sdk::EnumCameraObjects(&cameras), "Enumerate cameras"); }
        catch (...) { sdk::Release(); throw; }
    }
    ~Runtime() { if (cameras) cameras->Release(); sdk::Release(); }
};

struct Property {
    bool readable = false;
    bool writable = false;
    uint64_t current = 0;
    sdk::CrDataType type = sdk::CrDataType_Undefined;
    std::vector<uint64_t> values;
};

class Camera : public sdk::IDeviceCallback {
    sdk::CrDeviceHandle handle_ = 0;
    std::mutex mutex_;
    std::condition_variable changed_;
    bool connected_ = false;
    bool disconnected_ = false;
    uint32_t error_ = 0;
    std::vector<std::string> downloads_;
    std::optional<uint32_t> focus_result_;
    int timeout_;
    std::vector<std::pair<uint32_t, uint64_t>> originals_;
    std::string model_;

    void healthy() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (error_) check(static_cast<sdk::CrError>(error_), "Camera callback");
        if (disconnected_) throw std::runtime_error("Camera disconnected");
    }

public:
    explicit Camera(int timeout) : timeout_(timeout) {}
    ~Camera() {
        if (handle_) {
            sdk::Disconnect(handle_);
            {
                std::unique_lock<std::mutex> lock(mutex_);
                changed_.wait_for(lock, 5s, [&] { return disconnected_; });
            }
            sdk::ReleaseDevice(handle_);
        }
    }
    void connect(sdk::ICrCameraObjectInfo* info) {
        model_ = utf8(info->GetModel());
        if (model_ != "ILCE-7CM2") throw std::runtime_error("Expected ILCE-7CM2, found " + model_);
        if (info->GetIdType() != sdk::CAMERAOBJECTID_TYPE_USB)
            throw std::runtime_error("This implementation requires a USB PC Remote connection");
        check(sdk::Connect(info, this, &handle_, sdk::CrSdkControlMode_Remote, sdk::CrReconnecting_OFF), "Connect");
        std::unique_lock<std::mutex> lock(mutex_);
        if (!changed_.wait_for(lock, std::chrono::seconds(timeout_), [&] { return connected_ || error_ || disconnected_; }))
            throw std::runtime_error("Connection timed out");
        lock.unlock();
        healthy();
        // OnConnected can precede the initial property notification. Do not
        // publish an all-null snapshot or start configuring the camera yet.
        auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_);
        do {
            auto initial = properties();
            auto aperture = initial.find(sdk::CrDeviceProperty_FNumber);
            if (aperture != initial.end() && aperture->second.readable) return;
            std::this_thread::sleep_for(100ms);
        } while (std::chrono::steady_clock::now() < deadline);
        throw std::runtime_error("Initial camera properties timed out");
    }
    void OnConnected(sdk::DeviceConnectionVersioin) override {
        std::lock_guard<std::mutex> lock(mutex_);
        connected_ = true; changed_.notify_all();
    }
    void OnDisconnected(CrInt32u error) override {
        std::lock_guard<std::mutex> lock(mutex_);
        disconnected_ = true; error_ = error; changed_.notify_all();
    }
    void OnError(CrInt32u error) override {
        std::lock_guard<std::mutex> lock(mutex_);
        error_ = error; changed_.notify_all();
    }
    void OnWarning(CrInt32u warning) override {
        std::lock_guard<std::mutex> lock(mutex_);
        if (warning == sdk::CrWarning_FocusPosition_Result_OK ||
            warning == sdk::CrWarning_FocusPosition_Result_NG ||
            warning == sdk::CrWarning_FocusPosition_Result_Invalid) {
            focus_result_ = warning;
            changed_.notify_all();
        }
    }
    void OnCompleteDownload(CrChar* filename, CrInt32u) override {
        std::lock_guard<std::mutex> lock(mutex_);
        downloads_.push_back(utf8(filename)); changed_.notify_all();
    }

    std::map<uint32_t, Property> properties() {
        healthy();
        sdk::CrDeviceProperty* list = nullptr;
        CrInt32 count = 0;
        check(sdk::GetDeviceProperties(handle_, &list, &count), "Read properties");
        struct Release {
            sdk::CrDeviceHandle handle;
            sdk::CrDeviceProperty* list;
            ~Release() { if (list) sdk::ReleaseDeviceProperties(handle, list); }
        } release{handle_, list};
        std::map<uint32_t, Property> result;
        for (int i = 0; i < count; ++i) {
            auto& input = list[i];
            Property p;
            p.readable = input.IsGetEnableCurrentValue();
            p.writable = input.IsSetEnableCurrentValue();
            p.type = input.GetValueType();
            if (p.readable && p.type != sdk::CrDataType_STR) p.current = input.GetCurrentValue();
            auto base = static_cast<uint32_t>(p.type) & 0xfff;
            size_t width = base >= 1 && base <= 4 ? size_t(1) << (base - 1) : 0;
            if (width && input.GetValues()) {
                for (size_t offset = 0; offset + width <= input.GetValueSize(); offset += width) {
                    uint64_t value = 0;
                    std::memcpy(&value, input.GetValues() + offset, width);
                    p.values.push_back(value);
                }
            }
            result.emplace(input.GetCode(), p);
        }
        return result;
    }
    Property get(uint32_t code) {
        auto all = properties();
        auto found = all.find(code);
        if (found == all.end() || !found->second.readable)
            throw std::runtime_error("Required camera property unavailable: " + std::to_string(code));
        return found->second;
    }
    static bool permits(const Property& p, uint64_t value) {
        if (p.type & sdk::CrDataType_ArrayBit)
            return std::find(p.values.begin(), p.values.end(), value) != p.values.end();
        if (p.type & sdk::CrDataType_RangeBit)
            return p.values.size() >= 3 && value >= p.values[0] && value <= p.values[1]
                && (!p.values[2] || (value - p.values[0]) % p.values[2] == 0);
        return true;
    }
    void set(uint32_t code, uint64_t value, bool remember = true) {
        auto p = get(code);
        if (p.current == value) return;
        if (!p.writable || !permits(p, value))
            throw std::runtime_error("Camera rejects value " + std::to_string(value) + " for property " + std::to_string(code)
                + "; check camera mode, lens and menu settings");
        if (remember && std::none_of(originals_.begin(), originals_.end(), [code](auto entry) { return entry.first == code; }))
            originals_.emplace_back(code, p.current);
        sdk::CrDeviceProperty update;
        update.SetCode(code); update.SetValueType(p.type); update.SetCurrentValue(value);
        check(sdk::SetDeviceProperty(handle_, &update), "Set property " + std::to_string(code));
        auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_);
        do {
            std::this_thread::sleep_for(100ms);
            if (get(code).current == value) return;
        } while (std::chrono::steady_clock::now() < deadline);
        throw std::runtime_error("Property readback timed out: " + std::to_string(code));
    }
    void set_focus_position(uint64_t value) {
        // Focus position is an intentional lens adjustment. restore() restores
        // mode/priority but keeps this position.
        if (value > 65535) throw std::runtime_error("Focus position must be an integer from 0 to 65535 (not metres)");
        set(sdk::CrDeviceProperty_PriorityKeySettings, sdk::CrPriorityKey_PCRemote);
        set(sdk::CrDeviceProperty_FocusMode, sdk::CrFocus_MF);
        auto p = get(sdk::CrDeviceProperty_FocusPositionSetting);
        if (!p.writable || !permits(p, value))
            throw std::runtime_error("Absolute focus position is not writable with this camera/lens configuration");
        if (get(sdk::CrDeviceProperty_FocusDrivingStatus).current != sdk::CrFocusDrivingStatus_NotDriving)
            throw std::runtime_error("Focus is already moving; wait for it to stop before setting a position");
        if (get(sdk::CrDeviceProperty_FocusPositionCurrentValue).current == value) return;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            focus_result_.reset();
        }
        sdk::CrDeviceProperty update;
        update.SetCode(sdk::CrDeviceProperty_FocusPositionSetting);
        // The SDK sample writes a scalar UInt16 although its descriptor is a range.
        update.SetValueType(sdk::CrDataType_UInt16);
        update.SetCurrentValue(value);
        check(sdk::SetDeviceProperty(handle_, &update), "Set absolute focus position");
        // Do not compare the requested setpoint: only the completion notification
        // and actual lens driving status establish that the lens has stopped.
        auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_);
        try {
            std::this_thread::sleep_for(500ms);
            do {
                healthy();
                std::optional<uint32_t> result;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    result = focus_result_;
                }
                if (result && *result != sdk::CrWarning_FocusPosition_Result_OK)
                    throw std::runtime_error("Camera reported absolute focus movement failure: " + std::to_string(*result));
                if (result && get(sdk::CrDeviceProperty_FocusDrivingStatus).current == sdk::CrFocusDrivingStatus_NotDriving &&
                    get(sdk::CrDeviceProperty_FocusPositionCurrentValue).current == value) {
                    // The completion callback can precede the property update.
                    // Waiting for actual readback avoids returning a stale position.
                    return;
                }
                std::this_thread::sleep_for(100ms);
            } while (std::chrono::steady_clock::now() < deadline);
            throw std::runtime_error("Focus movement completion/readback timed out; cancellation requested");
        } catch (...) {
            sdk::SendCommand(handle_, sdk::CrCommandId_CancelFocusPosition, sdk::CrCommandParam_Down);
            sdk::SendCommand(handle_, sdk::CrCommandId_CancelFocusPosition, sdk::CrCommandParam_Up);
            throw;
        }
    }
    void prepare(uint64_t aspect) {
        // Camera-position priority can prevent remote changes. Restore this
        // last, after all other settings, using the existing originals_ stack.
        set(sdk::CrDeviceProperty_PriorityKeySettings, sdk::CrPriorityKey_PCRemote);
        set(sdk::CrDeviceProperty_ExposureProgramMode, sdk::CrExposure_A_AperturePriority);
        set(sdk::CrDeviceProperty_FocusMode, sdk::CrFocus_MF);
        set(sdk::CrDeviceProperty_DriveMode, sdk::CrDrive_Single);
        set(sdk::CrDeviceProperty_CompressionFileFormatStill, sdk::CrCompressionFileFormat_JPEG);
        set(sdk::CrDeviceProperty_FileType, sdk::CrFileType_Jpeg);
        set(sdk::CrDeviceProperty_AspectRatio, aspect);
        set(sdk::CrDeviceProperty_StillImageStoreDestination, sdk::CrStillImageStoreDestination_HostPC);
    }
    std::string restore() {
        std::ostringstream out;
        out << "{\"ok\":true,\"restore_errors\":[";
        bool first = true;
        for (auto it = originals_.rbegin(); it != originals_.rend(); ++it) {
            try { set(it->first, it->second, false); }
            catch (const std::exception& error) {
                if (!first) out << ',';
                first = false; out << quote(error.what());
            }
        }
        originals_.clear();
        out << "]}";
        return out.str();
    }
    std::string snapshot() {
        auto p = properties();
        std::ostringstream out;
        out << "{\"ok\":true,\"model\":" << quote(model_);
        auto value = [&](const char* name, uint32_t code, bool aperture_value = false) {
            out << ',' << quote(name) << ':';
            if (p.count(code) && p.at(code).readable) {
                if (aperture_value) out << aperture_json(p.at(code).current);
                else out << p.at(code).current;
            }
            else out << "null";
        };
        value("aperture", sdk::CrDeviceProperty_FNumber, true);
        value("aspect_ratio_raw", sdk::CrDeviceProperty_AspectRatio);
        value("focus_mode_raw", sdk::CrDeviceProperty_FocusMode);
        value("focus_position_raw", sdk::CrDeviceProperty_FocusPositionCurrentValue);
        value("focus_driving_status_raw", sdk::CrDeviceProperty_FocusDrivingStatus);
        value("focus_distance_raw", sdk::CrDeviceProperty_FocalDistanceInMeter);
        value("focal_length_raw", sdk::CrDeviceProperty_ZoomDistance);
        value("exposure_mode_raw", sdk::CrDeviceProperty_ExposureProgramMode);
        value("drive_mode_raw", sdk::CrDeviceProperty_DriveMode);
        value("file_type_raw", sdk::CrDeviceProperty_FileType);
        value("compression_raw", sdk::CrDeviceProperty_CompressionFileFormatStill);
        value("store_destination_raw", sdk::CrDeviceProperty_StillImageStoreDestination);
        value("priority_key_raw", sdk::CrDeviceProperty_PriorityKeySettings);
        out << ",\"supported_apertures\":[";
        auto aperture = p.find(sdk::CrDeviceProperty_FNumber);
        if (aperture != p.end() && (aperture->second.type & sdk::CrDataType_ArrayBit)) {
            bool first = true;
            for (auto raw : aperture->second.values) {
                if (!first) out << ',';
                first = false; out << aperture_json(raw);
            }
        }
        out << ']';
        auto descriptor = [&](const char* name, uint32_t code) {
            out << ',' << quote(name) << ':';
            auto found = p.find(code);
            if (found == p.end()) { out << "null"; return; }
            const auto& prop = found->second;
            out << "{\"type_raw\":" << static_cast<uint32_t>(prop.type)
                << ",\"readable\":" << (prop.readable ? "true" : "false")
                << ",\"writable\":" << (prop.writable ? "true" : "false")
                << ",\"values_raw\":[";
            for (size_t i = 0; i < prop.values.size(); ++i) { if (i) out << ','; out << prop.values[i]; }
            out << "]}";
        };
        descriptor("aperture_property", sdk::CrDeviceProperty_FNumber);
        descriptor("focus_position_property", sdk::CrDeviceProperty_FocusPositionSetting);
        descriptor("focus_distance_property", sdk::CrDeviceProperty_FocalDistanceInMeter);
        out << '}';
        return out.str();
    }
    std::string capture(const std::string& directory) {
        auto path = fs::absolute(fs::u8path(directory));
        if (!fs::is_directory(path) || !fs::is_empty(path))
            throw std::runtime_error("Capture destination must be an empty directory");
        auto wide = path.native();
        wchar_t prefix[] = L"";
        check(sdk::SetSaveInfo(handle_, wide.data(), prefix, -1), "Set save directory");
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!downloads_.empty()) throw std::runtime_error("Unexpected image download before shutter release");
        }
        // MF + single drive: release once, then wait for this image's transfer callback.
        auto down = sdk::SendCommand(handle_, sdk::CrCommandId_Release, sdk::CrCommandParam_Down);
        std::this_thread::sleep_for(35ms);
        auto up = sdk::SendCommand(handle_, sdk::CrCommandId_Release, sdk::CrCommandParam_Up);
        check(down, "Shutter down"); check(up, "Shutter up");
        std::unique_lock<std::mutex> lock(mutex_);
        if (!changed_.wait_for(lock, std::chrono::seconds(timeout_), [&] { return !downloads_.empty() || error_ || disconnected_; }))
            throw std::runtime_error("Image transfer timed out; shutter is not retried automatically");
        lock.unlock(); healthy(); lock.lock();
        if (downloads_.size() != 1) throw std::runtime_error("Expected exactly one transferred image");
        std::string file = downloads_.front(); downloads_.clear();
        return "{\"ok\":true,\"file\":" + quote(file) + "}";
    }
};

int main(int argc, char** argv) {
    try {
        bool list = argc == 2 && std::string(argv[1]) == "--list";
        if (!list && argc != 3) throw std::runtime_error("Usage: sonycam_bridge --list | CAMERA_INDEX TIMEOUT_SECONDS");
        Runtime runtime;
        if (list) {
            std::cout << "{\"ok\":true,\"cameras\":[";
            for (CrInt32u i = 0; runtime.cameras && i < runtime.cameras->GetCount(); ++i) {
                auto info = runtime.cameras->GetCameraObjectInfo(i);
                if (i) std::cout << ',';
                std::cout << "{\"index\":" << i << ",\"model\":" << quote(utf8(info->GetModel()))
                    << ",\"connection\":" << quote(utf8(info->GetConnectionTypeName())) << '}';
            }
            std::cout << "]}" << std::endl;
            return 0;
        }
        int index = std::stoi(argv[1]), timeout = std::stoi(argv[2]);
        if (!runtime.cameras || !runtime.cameras->GetCount())
            throw std::runtime_error("No camera found. Connect USB and select Remote Shoot (PC Remote); close Imaging Edge Remote.");
        if (index == -1 && runtime.cameras->GetCount() == 1) index = 0;
        if (index < 0 || static_cast<CrInt32u>(index) >= runtime.cameras->GetCount())
            throw std::runtime_error("Select camera_index explicitly when more than one camera is connected");
        if (timeout < 5) throw std::runtime_error("Timeout must be >= 5 seconds");
        Camera camera(timeout);
        camera.connect(const_cast<sdk::ICrCameraObjectInfo*>(runtime.cameras->GetCameraObjectInfo(index)));
        std::cout << camera.snapshot() << std::endl;
        std::string line;
        while (std::getline(std::cin, line)) {
            auto space = line.find(' ');
            auto command = line.substr(0, space);
            auto argument = space == std::string::npos ? "" : line.substr(space + 1);
            try {
                if (command == "quit") { std::cout << "{\"ok\":true}" << std::endl; return 0; }
                if (command == "state") std::cout << camera.snapshot() << std::endl;
                else if (command == "prepare") {
                    camera.prepare(std::stoull(argument)); std::cout << camera.snapshot() << std::endl;
                } else if (command == "aperture") {
                    camera.set(sdk::CrDeviceProperty_FNumber, std::stoull(argument));
                    std::cout << camera.snapshot() << std::endl;
                } else if (command == "focus") {
                    camera.set_focus_position(std::stoull(argument));
                    std::cout << camera.snapshot() << std::endl;
                } else if (command == "capture") std::cout << camera.capture(argument) << std::endl;
                else if (command == "restore") std::cout << camera.restore() << std::endl;
                else throw std::runtime_error("Unknown bridge command");
            } catch (const std::exception& error) {
                std::cout << "{\"ok\":false,\"error\":" << quote(error.what()) << '}' << std::endl;
            }
        }
        // Parent exited without the normal restore command.
        camera.restore();
        return 0;
    } catch (const std::exception& error) {
        std::cout << "{\"ok\":false,\"error\":" << quote(error.what()) << '}' << std::endl;
        return 1;
    }
}

