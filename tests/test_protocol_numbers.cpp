#include "protocol_numbers.h"
#include <iostream>
#include <utility>
#include <vector>

int main() {
    // Include intermediate stops offered by the real camera, not only the
    // seven requested stops: the whole candidate list is validated by Python.
    for (const auto& item : std::vector<std::pair<std::uint64_t, std::string>>{
             {280, "2.80"}, {320, "3.20"}, {350, "3.50"}, {400, "4.00"},
             {560, "5.60"}, {630, "6.30"}, {710, "7.10"}, {800, "8.00"},
             {1100, "11.00"}, {1600, "16.00"}, {2200, "22.00"}}) {
        if (aperture_json(item.first) != item.second) {
            std::cerr << "Incorrect JSON aperture for SDK value " << item.first << '\n';
            return 1;
        }
    }
    return 0;
}
