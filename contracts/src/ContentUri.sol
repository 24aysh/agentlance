// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Bounded Python 3.12.12 urllib.parse/isContentUri compatibility.
/// @dev Unicode tables below are from Python's Unicode 15.0.0 database. Original
/// URI bytes are never normalized; normalization only rejects authority delimiters.
library ContentUri {
    function isContentUri(string memory uri) internal pure returns (bool) {
        bytes memory data = bytes(uri);
        uint256 length = data.length;
        if (length > 2048) return false;
        uint256 start;
        // urlsplit strips leading C0 controls; Python whitespace is still rejected below.
        while (start < length && uint8(data[start]) <= 32) ++start;
        if (
            start + 8 <= length && lower(data[start]) == 104 && lower(data[start + 1]) == 116
                && lower(data[start + 2]) == 116 && lower(data[start + 3]) == 112
                && lower(data[start + 4]) == 115 && data[start + 5] == ":" && data[start + 6] == "/"
                && data[start + 7] == "/"
        ) {
            start += 8;
        } else if (
            start + 7 <= length && lower(data[start]) == 105 && lower(data[start + 1]) == 112
                && lower(data[start + 2]) == 102 && lower(data[start + 3]) == 115
                && data[start + 4] == ":" && data[start + 5] == "/" && data[start + 6] == "/"
        ) {
            start += 7;
        } else {
            return false;
        }
        uint256 end = start;
        while (end < length && data[end] != "/" && data[end] != "?" && data[end] != "#") ++end;
        if (!validText(data, start, end)) return false;
        for (uint256 i = end; i < length; ++i) {
            // A bare trailing '#' has an empty fragment, which the oracle accepts.
            if (data[i] == "#" && i + 1 != length) return false;
        }
        for (uint256 i = start; i < end; ++i) {
            if (data[i] == "@") {
                // Both username and password may be present but empty.
                if (i != start && !(i == start + 1 && data[start] == ":")) return false;
                start = i + 1;
                // A second @ would make the preceding userinfo nonempty.
                for (uint256 j = start; j < end; ++j) {
                    if (data[j] == "@") return false;
                }
                break;
            }
        }
        if (start == end) return false;
        uint256 portStart = end;
        if (data[start] == "[") {
            uint256 close = start + 1;
            while (close < end && data[close] != "]") ++close;
            if (close == end || !validIpLiteral(data, start + 1, close)) return false;
            if (close + 1 < end) {
                if (data[close + 1] != ":") return false;
                portStart = close + 2;
            }
        } else {
            for (uint256 i = start; i < end; ++i) {
                if (data[i] == "[" || data[i] == "]") return false;
                if (data[i] == ":" && portStart == end) {
                    if (i == start) return false;
                    portStart = i + 1;
                }
            }
        }
        if (portStart == end) return true;
        uint256 port;
        for (uint256 i = portStart; i < end; ++i) {
            uint8 digit = uint8(data[i]);
            if (digit < 48 || digit > 57) return false;
            port = port * 10 + digit - 48;
            if (port > 65535) return false;
        }
        return port != 0;
    }

    function lower(bytes1 character) private pure returns (uint8) {
        return uint8(character) | 32;
    }

    function validText(bytes memory data, uint256 authorityStart, uint256 authorityEnd)
        private
        pure
        returns (bool)
    {
        uint256 i;
        while (i < data.length) {
            uint256 first = i;
            uint32 code = uint8(data[i++]);
            if (code >= 128) {
                uint256 count;
                uint32 minimum;
                if (code >= 194 && code <= 223) {
                    code &= 31;
                    count = 1;
                    minimum = 128;
                } else if (code >= 224 && code <= 239) {
                    code &= 15;
                    count = 2;
                    minimum = 2048;
                } else if (code >= 240 && code <= 244) {
                    code &= 7;
                    count = 3;
                    minimum = 65536;
                } else {
                    return false;
                }
                if (i + count > data.length) return false;
                for (uint256 j; j < count; ++j) {
                    uint8 next = uint8(data[i++]);
                    if (next < 128 || next > 191) return false;
                    code = (code << 6) | (next & 63);
                }
                if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) {
                    return false;
                }
            }
            // Complete str.isspace() set, Unicode 15.0.0 (29 scalars).
            if (
                (code >= 9 && code <= 13) || (code >= 28 && code <= 32) || code == 0x85
                    || code == 0xa0 || code == 0x1680 || (code >= 0x2000 && code <= 0x200a)
                    || code == 0x2028 || code == 0x2029 || code == 0x202f || code == 0x205f
                    || code == 0x3000
            ) {
                return false;
            }
            if (first >= authorityStart && first < authorityEnd && isAuthorityDelimiter(code)) {
                return false;
            }
        }
        return true;
    }

    function isAuthorityDelimiter(uint32 code) private pure returns (bool) {
        // Exhaustive non-ASCII scalars whose NFKC contains one of /?#@:.
        // Combining normalization cannot create these ASCII delimiters otherwise.
        return (code >= 0x2047 && code <= 0x2049) || code == 0x2100 || code == 0x2101
            || code == 0x2105 || code == 0x2106 || code == 0x2a74 || code == 0xfe13
            || code == 0xfe16 || code == 0xfe55 || code == 0xfe56 || code == 0xfe5f
            || code == 0xfe6b || code == 0xff03 || code == 0xff0f || code == 0xff1a
            || code == 0xff1f || code == 0xff20;
    }

    function validIpLiteral(bytes memory data, uint256 start, uint256 end)
        private
        pure
        returns (bool)
    {
        if (start == end) return false;
        if (data[start] == "v") {
            uint256 i = start + 1;
            while (i < end && isHex(data[i])) ++i;
            return i > start + 1 && i + 1 < end && data[i] == ".";
        }
        // ipaddress accepts a nonempty arbitrary scope ID, but only one '%'.
        for (uint256 i = start; i < end; ++i) {
            if (data[i] == "%") {
                if (i + 1 == end) return false;
                for (uint256 j = i + 1; j < end; ++j) {
                    if (data[j] == "%") return false;
                }
                end = i;
                break;
            }
        }
        if (end - start > 45 || start == end) return false;
        uint256 parts;
        bool compressed;
        uint256 cursor = start;
        if (data[cursor] == ":") {
            if (cursor + 1 == end || data[cursor + 1] != ":") return false;
            compressed = true;
            cursor += 2;
        }
        while (cursor < end) {
            uint256 partStart = cursor;
            bool dotted;
            while (cursor < end && data[cursor] != ":") {
                if (data[cursor] == ".") dotted = true;
                ++cursor;
            }
            if (dotted) {
                if (cursor != end || !validIpv4(data, partStart, end)) return false;
                parts += 2;
            } else {
                if (cursor == partStart || cursor - partStart > 4) return false;
                for (uint256 i = partStart; i < cursor; ++i) {
                    if (!isHex(data[i])) return false;
                }
                ++parts;
            }
            if (cursor < end) {
                ++cursor;
                if (cursor < end && data[cursor] == ":") {
                    if (compressed) return false;
                    compressed = true;
                    ++cursor;
                } else if (cursor == end) {
                    return false;
                }
            }
        }
        return compressed ? parts < 8 : parts == 8;
    }

    function validIpv4(bytes memory data, uint256 start, uint256 end) private pure returns (bool) {
        uint256 count;
        uint256 cursor = start;
        while (cursor < end) {
            uint256 partStart = cursor;
            uint256 value;
            while (cursor < end && data[cursor] != ".") {
                uint8 digit = uint8(data[cursor++]);
                if (digit < 48 || digit > 57) return false;
                value = value * 10 + digit - 48;
                if (value > 255 || cursor - partStart > 3) return false;
            }
            if (cursor == partStart || (cursor - partStart > 1 && data[partStart] == "0")) {
                return false;
            }
            ++count;
            if (cursor < end && ++cursor == end) return false;
        }
        return count == 4;
    }

    function isHex(bytes1 character) private pure returns (bool) {
        uint8 code = uint8(character);
        return
            (code >= 48 && code <= 57) || (code >= 65 && code <= 70) || (code >= 97 && code <= 102);
    }
}
