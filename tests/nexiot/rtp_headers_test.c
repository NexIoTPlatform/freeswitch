#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rtp_normalizer_under_test.h"

enum { CAPACITY = 12 + 16384 + 4 + sizeof(char *), GUARD = 32 };
static unsigned int cases;

static size_t make_packet(unsigned char *packet, unsigned int cc, int extension,
                         unsigned int words, uint16_t profile, size_t payload,
                         unsigned int padding)
{
    size_t header = 12 + cc * 4 + (extension ? 4 + words * 4 : 0);
    size_t length = header + payload + padding;
    assert(length <= CAPACITY);
    memset(packet, 0, length);
    packet[0] = (unsigned char)(0x80 | (padding ? 0x20 : 0) | (extension ? 0x10 : 0) | cc);
    packet[1] = 0x80 | 99;
    for (size_t i = 2; i < 12; ++i) packet[i] = (unsigned char)(i * 17 + 3);
    for (size_t i = 12; i < 12 + cc * 4; ++i) packet[i] = (unsigned char)(i * 7);
    if (extension) {
        size_t offset = 12 + cc * 4;
        packet[offset] = (unsigned char)(profile >> 8);
        packet[offset + 1] = (unsigned char)profile;
        packet[offset + 2] = (unsigned char)(words >> 8);
        packet[offset + 3] = (unsigned char)words;
        for (size_t i = offset + 4; i < header; ++i) packet[i] = (unsigned char)(i * 13);
    }
    for (size_t i = 0; i < payload; ++i) packet[header + i] = (unsigned char)(i * 29 + 0x37);
    if (padding) {
        memset(packet + header + payload, 0xa5, padding);
        packet[length - 1] = (unsigned char)padding;
    }
    return length;
}

/* Independently parse the bytes a receiver would see on the forwarded wire. */
static size_t wire_header_size(const unsigned char *packet, size_t length)
{
    size_t offset;
    assert(length >= 12);
    offset = 12 + (packet[0] & 15) * 4;
    assert(offset <= length);
    if (packet[0] & 16) {
        unsigned int words;
        assert(length - offset >= 4);
        words = packet[offset + 2] * 256u + packet[offset + 3];
        offset += 4 + words * 4;
        assert(offset <= length);
    }
    return offset;
}

static void valid_case(unsigned int cc, int extension, unsigned int words,
                       uint16_t profile, size_t payload, unsigned int padding)
{
    unsigned char *memory = malloc(CAPACITY + 2 * GUARD);
    unsigned char *original = malloc(CAPACITY);
    unsigned char *queued = malloc(CAPACITY);
    unsigned char *packet = memory + GUARD;
    size_t before, length, header, queued_length, frame_data_length;
    assert(memory && original && queued);
    memset(memory, 0xd7, CAPACITY + 2 * GUARD);
    before = make_packet(packet, cc, extension, words, profile, payload, padding);
    memcpy(original, packet, before);
    header = wire_header_size(original, before);
    length = before;
    assert(rtp_normalize_read_packet(packet, &length, CAPACITY));
    assert(length == 12 + payload + padding);
    assert(packet[0] == (original[0] & 0xe0));
    assert(memcmp(packet + 1, original + 1, 11) == 0);
    assert(memcmp(packet + 12, original + header, payload + padding) == 0);
    assert(wire_header_size(packet, length) == 12);
    for (size_t i = 0; i < GUARD; ++i) {
        assert(memory[i] == 0xd7);
        assert(memory[GUARD + CAPACITY + i] == 0xd7);
    }
    for (size_t i = before; i < CAPACITY; ++i) assert(packet[i] == 0xd7);

    /* The jitter buffer retains the normalized bytes and their complete length.
     * A raw frame and a codec frame now agree on payload start and length. */
    memcpy(queued, packet, length);
    queued_length = length;
    frame_data_length = queued_length - 12;
    assert(memcmp(queued + wire_header_size(queued, queued_length),
                  original + header, payload + padding) == 0);
    assert(frame_data_length == payload + padding);
    assert(frame_data_length + 12 == queued_length);

    /* Reprocessing a queued or already-normalized packet cannot remove bytes twice. */
    assert(rtp_normalize_read_packet(packet, &length, CAPACITY));
    assert(length == queued_length && memcmp(packet, queued, length) == 0);
    free(queued);
    free(original);
    free(memory);
    ++cases;
}

static void rejected_case(const unsigned char *input, size_t available, size_t capacity)
{
    size_t allocation = capacity < CAPACITY ? capacity : CAPACITY;
    unsigned char *packet = malloc(allocation ? allocation : 1);
    unsigned char *before = malloc(allocation ? allocation : 1);
    size_t length = available;
    assert(packet && before);
    memcpy(packet, input, allocation);
    memcpy(before, input, allocation);
    assert(!rtp_normalize_read_packet(packet, &length, capacity));
    assert(length == available);
    assert(memcmp(packet, before, allocation) == 0);
    free(before);
    free(packet);
    ++cases;
}

static void negative_cases(void)
{
    unsigned char packet[CAPACITY];
    size_t length;
    memset(packet, 0, sizeof(packet));
    packet[0] = 0x80;
    for (size_t n = 0; n < 12; ++n) rejected_case(packet, n, n);
    length = make_packet(packet, 15, 0, 0, 0, 0, 0);
    rejected_case(packet, length - 1, length - 1);
    for (unsigned int cc = 0; cc <= 15; ++cc) {
        length = make_packet(packet, cc, 1, 1, 0xbede, 1, 0);
        for (size_t extra = 0; extra < 4; ++extra) {
            size_t n = 12 + cc * 4 + extra;
            rejected_case(packet, n, n);
        }
        rejected_case(packet, length - 2, length - 2); /* extension one byte short */
        packet[12 + cc * 4 + 2] = 0xff;
        packet[12 + cc * 4 + 3] = 0xff;
        rejected_case(packet, length, length);
    }
    length = make_packet(packet, 0, 1, 1, 0xbede, 1, 0);
    rejected_case(packet, CAPACITY + 1, CAPACITY);
    rejected_case(packet, SIZE_MAX, CAPACITY);
    for (unsigned int version = 0; version < 4; ++version) {
        if (version == 2) continue;
        packet[0] = (unsigned char)(version << 6);
        rejected_case(packet, length, length);
    }
}

int main(void)
{
    const size_t payloads[] = {0, 1, 7, 8, 9, 1184, 1200};
    const unsigned int paddings[] = {0, 1, 4, 255};
    /* The production Jami case: 1204 bytes, BEDE/one word adds eight bytes.
     * The resulting 1196-byte packet must have X=0 and retain all 1184 payload bytes. */
    valid_case(0, 1, 1, 0xbede, 1184, 0);
    for (unsigned int cc = 0; cc <= 15; ++cc) {
        for (size_t p = 0; p < sizeof(payloads) / sizeof(payloads[0]); ++p) {
            for (size_t padding = 0; padding < sizeof(paddings) / sizeof(paddings[0]); ++padding) {
                valid_case(cc, 0, 0, 0, payloads[p], paddings[padding]);
                valid_case(cc, 1, 0, 0xbede, payloads[p], paddings[padding]);
                valid_case(cc, 1, 1, 0xbede, payloads[p], paddings[padding]);
                valid_case(cc, 1, 7, 0x1000, payloads[p], paddings[padding]);
            }
        }
    }
    valid_case(0, 1, 4093, 0xbede, 0, 0);
    valid_case(0, 0, 0, 0, CAPACITY - 12, 0);
    negative_cases();
    printf("PASS: %u RTP normalization/forwarding/boundary cases\n", cases);
    return 0;
}
