cmake_minimum_required(VERSION 3.20)
project(sonycam LANGUAGES CXX)

set(SONY_SDK_ROOT "" CACHE PATH "Extracted Sony Camera Remote SDK directory")
if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "This bridge currently targets Windows x64.")
endif()
find_path(SONY_SDK_INCLUDE CameraRemote_SDK.h
    HINTS "${SONY_SDK_ROOT}/app/CRSDK" "${SONY_SDK_ROOT}/include" REQUIRED NO_DEFAULT_PATH)
find_library(SONY_SDK_LIBRARY Cr_Core
    HINTS "${SONY_SDK_ROOT}/external/crsdk" "${SONY_SDK_ROOT}/lib" REQUIRED NO_DEFAULT_PATH)
get_filename_component(SONY_SDK_RUNTIME "${SONY_SDK_LIBRARY}" DIRECTORY)

add_executable(sonycam_bridge native/bridge.cpp)
target_compile_features(sonycam_bridge PRIVATE cxx_std_17)
target_compile_definitions(sonycam_bridge PRIVATE UNICODE _UNICODE NOMINMAX)
target_include_directories(sonycam_bridge SYSTEM PRIVATE "${SONY_SDK_INCLUDE}")
target_link_libraries(sonycam_bridge PRIVATE "${SONY_SDK_LIBRARY}")
if(MSVC)
    target_compile_options(sonycam_bridge PRIVATE /W4 /utf-8 /EHsc)
endif()
add_custom_command(TARGET sonycam_bridge POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_directory "${SONY_SDK_RUNTIME}" "$<TARGET_FILE_DIR:sonycam_bridge>")

include(CTest)
if(BUILD_TESTING)
    add_executable(test_protocol_numbers tests/test_protocol_numbers.cpp)
    target_include_directories(test_protocol_numbers PRIVATE native)
    target_compile_features(test_protocol_numbers PRIVATE cxx_std_17)
    if(MSVC)
        target_compile_options(test_protocol_numbers PRIVATE /W4 /utf-8 /EHsc)
    endif()
    add_test(NAME protocol_numbers COMMAND test_protocol_numbers)
endif()

