// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {ContentUri} from "../src/ContentUri.sol";

interface UriVm {
    function readFile(string calldata path) external view returns (string memory);
    function parseJson(string calldata json, string calldata key)
        external
        pure
        returns (bytes memory);
}

/// @dev Also deployed by the independent Python differential test.
contract ContentUriHarness {
    function isContentUri(string memory uri) external pure returns (bool) {
        return ContentUri.isContentUri(uri);
    }
}

contract ContentUriTest {
    UriVm private constant vm = UriVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    // parseJson encodes JSON object fields in alphabetical order.
    struct UriCase {
        string id;
        string uri;
        bool valid;
    }

    struct RawCase {
        bytes hexValue;
        string id;
        bool valid;
    }

    function testReviewedPythonUriBoundaries() public view {
        string memory fixture = vm.readFile("specs/fixtures/layer-3-uri.json");
        UriCase[] memory cases = abi.decode(vm.parseJson(fixture, ".cases"), (UriCase[]));
        for (uint256 i; i < cases.length; ++i) {
            require(ContentUri.isContentUri(cases[i].uri) == cases[i].valid, cases[i].id);
        }
        RawCase[] memory rawCases = abi.decode(vm.parseJson(fixture, ".rawCases"), (RawCase[]));
        for (uint256 i; i < rawCases.length; ++i) {
            require(
                ContentUri.isContentUri(string(rawCases[i].hexValue)) == rawCases[i].valid,
                rawCases[i].id
            );
        }
    }

    function testFuzzPermissiveAsciiPath(bytes memory source) public pure {
        if (source.length > 2038) return;
        // Any printable ASCII path is permitted except a nonempty fragment.
        bytes memory path = new bytes(source.length);
        for (uint256 i; i < source.length; ++i) {
            uint8 character = 33 + uint8(source[i]) % 94;
            path[i] = character == 35 ? bytes1(uint8(36)) : bytes1(character);
        }
        require(ContentUri.isContentUri(string(abi.encodePacked("https://x/", path))), "ASCII path");
    }

    function testFuzzPort(uint32 port) public pure {
        bytes memory digits = new bytes(10);
        uint256 position = 10;
        uint256 rest = port;
        do {
            digits[--position] = bytes1(uint8(48 + rest % 10));
            rest /= 10;
        } while (rest != 0);
        bytes memory uri = abi.encodePacked("https://x:000", new bytes(10 - position));
        for (uint256 i = position; i < 10; ++i) {
            uri[13 + i - position] = digits[i];
        }
        require(ContentUri.isContentUri(string(uri)) == (port > 0 && port <= 65535), "port bounds");
    }
}
