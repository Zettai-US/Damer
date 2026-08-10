cmake_minimum_required(VERSION 3.20)

project(CXLDataMovement LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17 CACHE STRING "C++ standard to conform to")
set(CMAKE_CXX_STANDARD_REQUIRED YES)
set(CMAKE_CXX_EXTENSIONS NO)
set(LLVM_LIT_ARGS "-sv" CACHE STRING "Default options for lit")

option(CXL_DATA_MOVEMENT_ENABLE_MLIR
  "Build the legacy MLIR/CIRCT cxl-data-movement-opt backend" OFF)
option(CXL_DATA_MOVEMENT_INCLUDE_TESTS "Generate lit test targets" ON)

if(NOT CXL_DATA_MOVEMENT_ENABLE_MLIR)
  message(STATUS "MLIR/CIRCT backend disabled; eBPF middleware tools are Python entrypoints under tools/")
  message(STATUS "Run regression tests directly with: llvm-lit -av test")
  return()
endif()

find_package(LLVM REQUIRED CONFIG)
find_package(MLIR REQUIRED CONFIG)
find_package(CIRCT REQUIRED CONFIG)

message(STATUS "Using LLVMConfig.cmake in: ${LLVM_DIR}")
message(STATUS "Using MLIRConfig.cmake in: ${MLIR_DIR}")
message(STATUS "Using CIRCTConfig.cmake in: ${CIRCT_DIR}")

list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${CIRCT_CMAKE_DIR}")

include(TableGen)
include(AddLLVM)
include(AddMLIR)
include(AddCIRCT)
include(HandleLLVMOptions)

include_directories(${LLVM_INCLUDE_DIRS})
include_directories(${MLIR_INCLUDE_DIRS})
include_directories(${CIRCT_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)
include_directories(${CMAKE_CURRENT_BINARY_DIR}/include)

set(CXL_DATA_MOVEMENT_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR})
set(CXL_DATA_MOVEMENT_BINARY_DIR ${CMAKE_CURRENT_BINARY_DIR})

if(NOT LLVM_EXTERNAL_LIT AND EXISTS "${LLVM_TOOLS_BINARY_DIR}/llvm-lit")
  set(LLVM_EXTERNAL_LIT "${LLVM_TOOLS_BINARY_DIR}/llvm-lit" CACHE STRING
      "Command used to spawn lit")
endif()

add_subdirectory(include)
add_subdirectory(lib)
add_subdirectory(tools)
if(CXL_DATA_MOVEMENT_INCLUDE_TESTS)
  add_subdirectory(test)
endif()
