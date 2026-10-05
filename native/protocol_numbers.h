#pragma once

#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>

// SDK F-numbers are integers in hundredths. Serialize the decimal directly;
// double division can turn 320 into 3.1999999999999997 on MSVC.
inline std::string aperture_json(std::uint64_t raw) {
    std::ostringstream out;
    out << raw / 100 << '.' << std::setw(2) << std::setfill('0') << raw % 100;
    return out.str();
}
