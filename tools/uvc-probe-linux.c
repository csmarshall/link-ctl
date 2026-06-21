/*
 * uvc-probe-linux.c — Probe UVC extension/terminal units on Insta360 Link cameras.
 *
 * Linux port of tools/uvc-probe.m (macOS/IOKit). Uses libusb control transfers
 * without claiming the VideoControl interface so uvcvideo can keep streaming.
 *
 * Usage:
 *   tools/uvc-probe-linux snapshot
 *   tools/uvc-probe-linux watch [ms]
 *   tools/uvc-probe-linux server
 *   tools/uvc-probe-linux get <unit> <sel> <len>
 *   tools/uvc-probe-linux set <unit> <sel> <hex>
 *   tools/uvc-probe-linux getset <unit> <sel> <hex>
 *   tools/uvc-probe-linux --detach snapshot   (detach kernel driver first)
 *
 * Build:
 *   make -C tools uvc-probe-linux
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
#include <libusb-1.0/libusb.h>

#define INSTA360_VID 0x2E1A
#define VC_IFACE_NUM 0

/* Supported Insta360 Link USB product IDs */
static const uint16_t SUPPORTED_PIDS[] = {
    0x4C01, /* OG Link */
    0x4C04, /* Link 2 */
    0x4C02, /* Link 2C (unverified) */
    0x4C03, /* Link 2 Pro (unverified) */
};

#define MAX_ENTRIES 512
#define MAX_LEN 256

typedef struct {
    uint8_t unit;
    uint8_t sel;
    uint8_t len;
    uint8_t data[MAX_LEN];
} Entry;

static Entry g_known[MAX_ENTRIES];
static int g_nknown = 0;
static int g_detach = 0;
static int g_detached = 0;
static libusb_device_handle *g_handle = NULL;

static const uint8_t UNITS[] = {1, 2, 3, 4, 5, 9, 10, 11, 12, 13, 14, 15};
static const uint8_t TRY_LENS[] = {1, 2, 4, 8, 16, 32, 52, 61, 64};

static int pid_supported(uint16_t pid)
{
    for (size_t i = 0; i < sizeof(SUPPORTED_PIDS) / sizeof(SUPPORTED_PIDS[0]); i++)
        if (SUPPORTED_PIDS[i] == pid)
            return 1;
    return 0;
}

static libusb_device_handle *open_insta360(libusb_context *ctx)
{
    libusb_device **list = NULL;
    ssize_t cnt = libusb_get_device_list(ctx, &list);
    if (cnt < 0)
        return NULL;

    libusb_device_handle *handle = NULL;
    for (ssize_t i = 0; i < cnt; i++) {
        struct libusb_device_descriptor desc;
        if (libusb_get_device_descriptor(list[i], &desc) != 0)
            continue;
        if (desc.idVendor != INSTA360_VID || !pid_supported(desc.idProduct))
            continue;

        int r = libusb_open(list[i], &handle);
        if (r != 0) {
            fprintf(stderr, "libusb_open failed for %04x:%04x: %s\n",
                    desc.idVendor, desc.idProduct, libusb_strerror(r));
            continue;
        }

        if (g_detach) {
            if (libusb_kernel_driver_active(handle, VC_IFACE_NUM) == 1) {
                r = libusb_detach_kernel_driver(handle, VC_IFACE_NUM);
                if (r == 0)
                    g_detached = 1;
                else
                    fprintf(stderr, "warning: detach VC iface failed: %s\n",
                            libusb_strerror(r));
            }
        }

        g_handle = handle;
        libusb_free_device_list(list, 1);
        return handle;
    }

    libusb_free_device_list(list, 1);
    return NULL;
}

static void cleanup_handle(libusb_device_handle *h)
{
    if (!h)
        return;
    if (g_detached) {
        if (libusb_kernel_driver_active(h, VC_IFACE_NUM) == 0)
            libusb_attach_kernel_driver(h, VC_IFACE_NUM);
        g_detached = 0;
    }
    libusb_close(h);
    g_handle = NULL;
}

static int ctrl_get(libusb_device_handle *h, uint8_t unit, uint8_t sel,
                    void *buf, uint16_t len, uint8_t breq)
{
    int r = libusb_control_transfer(
        h,
        0xA1,
        breq,
        (uint16_t)sel << 8,
        (uint16_t)((unit << 8) | VC_IFACE_NUM),
        (unsigned char *)buf,
        len,
        1000);
  return (r >= 0) ? 0 : -1;
}

static int ctrl_set(libusb_device_handle *h, uint8_t unit, uint8_t sel,
                    void *buf, uint16_t len)
{
    int r = libusb_control_transfer(
        h,
        0x21,
        0x01,
        (uint16_t)sel << 8,
        (uint16_t)((unit << 8) | VC_IFACE_NUM),
        (unsigned char *)buf,
        len,
        1000);
    return (r >= 0) ? 0 : -1;
}

static void discover(libusb_device_handle *h)
{
    g_nknown = 0;
    for (size_t ui = 0; ui < sizeof(UNITS); ui++) {
        uint8_t unit = UNITS[ui];
        for (uint8_t sel = 0x01; sel <= 0x40; sel++) {
            if (g_nknown >= MAX_ENTRIES)
                return;

            uint8_t buf[MAX_LEN] = {0};
            uint8_t found_len = 0;

            uint8_t lbuf[2] = {0, 0};
            if (ctrl_get(h, unit, sel, lbuf, 2, 0x85) == 0) {
                uint16_t l = (uint16_t)lbuf[0] | ((uint16_t)lbuf[1] << 8);
                if (l > 0 && l <= MAX_LEN) {
                    if (ctrl_get(h, unit, sel, buf, (uint16_t)l, 0x81) == 0)
                        found_len = (uint8_t)l;
                }
            }

            if (!found_len) {
                for (size_t li = 0; li < sizeof(TRY_LENS); li++) {
                    memset(buf, 0, sizeof(buf));
                    if (ctrl_get(h, unit, sel, buf, TRY_LENS[li], 0x81) == 0) {
                        found_len = TRY_LENS[li];
                        break;
                    }
                }
            }

            if (!found_len)
                continue;

            Entry *e = &g_known[g_nknown++];
            e->unit = unit;
            e->sel = sel;
            e->len = found_len;
            memcpy(e->data, buf, found_len);
        }
    }
}

static void refresh(libusb_device_handle *h, Entry *entries, int n)
{
    for (int i = 0; i < n; i++) {
        Entry *e = &entries[i];
        uint8_t buf[MAX_LEN] = {0};
        if (ctrl_get(h, e->unit, e->sel, buf, e->len, 0x81) == 0)
            memcpy(e->data, buf, e->len);
    }
}

static void print_entries(Entry *entries, int n)
{
    for (int i = 0; i < n; i++) {
        Entry *e = &entries[i];
        printf("unit=%2d sel=0x%02x len=%d hex=", e->unit, e->sel, e->len);
        for (int j = 0; j < e->len; j++)
            printf("%02x", e->data[j]);
        printf("\n");
    }
    fflush(stdout);
}

static void print_ts(void)
{
    time_t t = time(NULL);
    struct tm *tm = localtime(&t);
    printf("[%02d:%02d:%02d] ", tm->tm_hour, tm->tm_min, tm->tm_sec);
}

static void watch(libusb_device_handle *h, int interval_ms)
{
    Entry cur[MAX_ENTRIES];
    memcpy(cur, g_known, (size_t)g_nknown * sizeof(Entry));

    fprintf(stderr, "Watching %d selectors (interval %dms).\n", g_nknown, interval_ms);

    for (;;) {
        Entry nxt[MAX_ENTRIES];
        memcpy(nxt, cur, (size_t)g_nknown * sizeof(Entry));
        refresh(h, nxt, g_nknown);

        for (int i = 0; i < g_nknown; i++) {
            if (memcmp(cur[i].data, nxt[i].data, cur[i].len) == 0)
                continue;
            print_ts();
            printf("CHANGED unit=%2d sel=0x%02x ", cur[i].unit, cur[i].sel);
            for (int j = 0; j < cur[i].len; j++)
                printf("%02x", cur[i].data[j]);
            printf(" -> ");
            for (int j = 0; j < nxt[i].len; j++)
                printf("%02x", nxt[i].data[j]);
            printf("\n");
            fflush(stdout);
        }
        memcpy(cur, nxt, (size_t)g_nknown * sizeof(Entry));
        usleep((useconds_t)interval_ms * 1000);
    }
}

static void server(libusb_device_handle *h)
{
    fprintf(stderr, "server: ready (%d selectors). Send newline to trigger snapshot.\n",
            g_nknown);
    fflush(stderr);

    char buf[64];
    while (fgets(buf, sizeof(buf), stdin)) {
        Entry snap[MAX_ENTRIES];
        memcpy(snap, g_known, (size_t)g_nknown * sizeof(Entry));
        refresh(h, snap, g_nknown);
        print_entries(snap, g_nknown);
        printf("END\n");
        fflush(stdout);
    }
}

static int parse_hex(const char *hex, uint8_t *out, size_t out_max)
{
    size_t hlen = strlen(hex);
    if (hlen % 2 != 0)
        return -1;
    size_t dlen = hlen / 2;
    if (dlen > out_max)
        return -1;
    for (size_t i = 0; i < dlen; i++) {
        unsigned int b;
        if (sscanf(hex + i * 2, "%2x", &b) != 1)
            return -1;
        out[i] = (uint8_t)b;
    }
    return (int)dlen;
}

int main(int argc, char *argv[])
{
    int argi = 1;
    if (argi < argc && strcmp(argv[argi], "--detach") == 0) {
        g_detach = 1;
        argi++;
    }

    const char *mode = (argi < argc) ? argv[argi++] : "snapshot";
    int interval_ms = (argi < argc) ? atoi(argv[argi]) : 200;
    if (interval_ms < 50)
        interval_ms = 50;

    libusb_context *ctx = NULL;
    if (libusb_init(&ctx) != 0) {
        fputs("libusb_init failed\n", stderr);
        return 1;
    }

    libusb_device_handle *h = open_insta360(ctx);
    if (!h) {
        fputs("Insta360 Link VideoControl device not found\n", stderr);
        libusb_exit(ctx);
        return 1;
    }

    if (strcmp(mode, "get") == 0) {
        if (argi + 3 > argc) {
            fprintf(stderr, "Usage: %s [--detach] get <unit> <sel> <len>\n", argv[0]);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        uint8_t unit = (uint8_t)strtol(argv[argi], NULL, 0);
        uint8_t sel = (uint8_t)strtol(argv[argi + 1], NULL, 0);
        uint8_t len = (uint8_t)strtol(argv[argi + 2], NULL, 0);
        uint8_t buf[MAX_LEN] = {0};
        if (ctrl_get(h, unit, sel, buf, len, 0x81) != 0) {
            fprintf(stderr, "GET_CUR failed: unit=%d sel=0x%02x len=%d\n", unit, sel, len);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        for (int i = 0; i < len; i++)
            printf("%02x", buf[i]);
        printf("\n");
    } else if (strcmp(mode, "set") == 0) {
        if (argi + 3 > argc) {
            fprintf(stderr, "Usage: %s [--detach] set <unit> <sel> <hex>\n", argv[0]);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        uint8_t unit = (uint8_t)strtol(argv[argi], NULL, 0);
        uint8_t sel = (uint8_t)strtol(argv[argi + 1], NULL, 0);
        uint8_t buf[MAX_LEN] = {0};
        int dlen = parse_hex(argv[argi + 2], buf, MAX_LEN);
        if (dlen < 0) {
            fprintf(stderr, "invalid hex payload\n");
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        if (ctrl_set(h, unit, sel, buf, (uint16_t)dlen) != 0) {
            fprintf(stderr, "SET_CUR failed: unit=%d sel=0x%02x\n", unit, sel);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        printf("OK\n");
    } else if (strcmp(mode, "getset") == 0) {
        if (argi + 3 > argc) {
            fprintf(stderr, "Usage: %s [--detach] getset <unit> <sel> <hex>\n", argv[0]);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        uint8_t unit = (uint8_t)strtol(argv[argi], NULL, 0);
        uint8_t sel = (uint8_t)strtol(argv[argi + 1], NULL, 0);
        uint8_t buf[MAX_LEN] = {0};
        int dlen = parse_hex(argv[argi + 2], buf, MAX_LEN);
        if (dlen < 0 || ctrl_set(h, unit, sel, buf, (uint16_t)dlen) != 0) {
            fprintf(stderr, "SET_CUR failed: unit=%d sel=0x%02x\n", unit, sel);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        printf("SET: ");
        for (int i = 0; i < dlen; i++)
            printf("%02x", buf[i]);
        printf("\n");
        usleep(200000);
        memset(buf, 0, sizeof(buf));
        if (ctrl_get(h, unit, sel, buf, (uint16_t)dlen, 0x81) != 0) {
            fprintf(stderr, "GET_CUR failed after SET\n");
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
        printf("GET: ");
        for (int i = 0; i < dlen; i++)
            printf("%02x", buf[i]);
        printf("\n");
    } else {
        fprintf(stderr, "Discovering selectors...\n");
        discover(h);
        fprintf(stderr, "Found %d readable selectors.\n", g_nknown);

        if (strcmp(mode, "snapshot") == 0) {
            print_entries(g_known, g_nknown);
        } else if (strcmp(mode, "watch") == 0) {
            watch(h, interval_ms);
        } else if (strcmp(mode, "server") == 0) {
            server(h);
        } else {
            fprintf(stderr,
                    "Usage: %s [--detach] [snapshot|watch [ms]|server|get|set|getset]\n",
                    argv[0]);
            cleanup_handle(h);
            libusb_exit(ctx);
            return 1;
        }
    }

    cleanup_handle(h);
    libusb_exit(ctx);
    return 0;
}
