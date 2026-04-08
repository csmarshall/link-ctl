/*
 * uvc-probe.m — Probe and watch all UVC extension unit selectors on the Insta360 Link
 *
 * Usage:
 *   sudo tools/uvc-probe snapshot         # one-shot: print all readable selectors
 *   sudo tools/uvc-probe watch [ms]       # poll every ms (default 200), print changes
 *   sudo tools/uvc-probe server           # pipe mode: newline → snapshot + "END\n"
 *
 * Compile:
 *   clang -o tools/uvc-probe tools/uvc-probe.m \
 *       -framework IOKit -framework CoreFoundation -framework Foundation -ObjC
 *
 * First run discovers which unit/selector combinations respond (GET_LEN then
 * GET_CUR with 1/2/4/8 bytes). Subsequent polls only re-read known entries.
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <time.h>
#import  <Foundation/Foundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/usb/USBSpec.h>

#define INSTA360_VID  0x2E1A
#define INSTA360_PID  0x4C01
#define VC_IFACE_NUM  0

#define MAX_ENTRIES 512
#define MAX_LEN      16

typedef struct {
    uint8_t unit;
    uint8_t sel;
    uint8_t len;
    uint8_t data[MAX_LEN];
} Entry;

static Entry  g_known[MAX_ENTRIES];
static int    g_nknown = 0;

// ── IOKit helpers ─────────────────────────────────────────────────────────────

static IOUSBInterfaceInterface **find_vc_interface(void)
{
    io_iterator_t iter = 0;
    IOServiceGetMatchingServices(kIOMainPortDefault,
        IOServiceMatching("IOUSBHostInterface"), &iter);

    io_service_t svc = 0;
    while ((svc = IOIteratorNext(iter))) {
        CFNumberRef vr = (CFNumberRef)IORegistryEntryCreateCFProperty(svc, CFSTR("idVendor"),        NULL, 0);
        CFNumberRef pr = (CFNumberRef)IORegistryEntryCreateCFProperty(svc, CFSTR("idProduct"),       NULL, 0);
        CFNumberRef nr = (CFNumberRef)IORegistryEntryCreateCFProperty(svc, CFSTR("bInterfaceNumber"),NULL, 0);
        int vid=-1, pid=-1, num=-1;
        if (vr) { CFNumberGetValue(vr, kCFNumberIntType, &vid); CFRelease(vr); }
        if (pr) { CFNumberGetValue(pr, kCFNumberIntType, &pid); CFRelease(pr); }
        if (nr) { CFNumberGetValue(nr, kCFNumberIntType, &num); CFRelease(nr); }
        if (vid != INSTA360_VID || pid != INSTA360_PID || num != VC_IFACE_NUM) {
            IOObjectRelease(svc); continue;
        }
        IOObjectRelease(iter);

        IOCFPlugInInterface **plugin = NULL; SInt32 score;
        IOCreatePlugInInterfaceForService(svc,
            kIOUSBInterfaceUserClientTypeID, kIOCFPlugInInterfaceID, &plugin, &score);
        IOObjectRelease(svc);
        if (!plugin) return NULL;

        IOUSBInterfaceInterface **iface = NULL;
        (*plugin)->QueryInterface(plugin,
            CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID), (LPVOID *)&iface);
        (*plugin)->Release(plugin);
        return iface;
    }
    IOObjectRelease(iter);
    return NULL;
}

static int ctrl_get(IOUSBInterfaceInterface **iface,
                    uint8_t unit, uint8_t sel,
                    void *buf, uint16_t len, uint8_t breq)
{
    IOUSBDevRequest req = {
        .bmRequestType = 0xa1,
        .bRequest      = breq,
        .wValue        = (uint16_t)sel << 8,
        .wIndex        = ((uint16_t)unit << 8) | VC_IFACE_NUM,
        .wLength       = len,
        .pData         = buf,
    };
    return (*iface)->ControlRequest(iface, 0, &req) == kIOReturnSuccess;
}

static int ctrl_set(IOUSBInterfaceInterface **iface,
                    uint8_t unit, uint8_t sel,
                    void *buf, uint16_t len)
{
    IOUSBDevRequest req = {
        .bmRequestType = 0x21,
        .bRequest      = 0x01,   // SET_CUR
        .wValue        = (uint16_t)sel << 8,
        .wIndex        = ((uint16_t)unit << 8) | VC_IFACE_NUM,
        .wLength       = len,
        .pData         = buf,
    };
    return (*iface)->ControlRequest(iface, 0, &req) == kIOReturnSuccess;
}

// ── Discovery ─────────────────────────────────────────────────────────────────

static const uint8_t UNITS[]    = {1, 2, 3, 4, 5, 9, 10, 11, 12, 13, 14, 15};
static const uint8_t TRY_LENS[] = {1, 2, 4, 8};

static void discover(IOUSBInterfaceInterface **iface)
{
    g_nknown = 0;
    for (int ui = 0; ui < (int)(sizeof(UNITS)/sizeof(UNITS[0])); ui++) {
        uint8_t unit = UNITS[ui];
        for (uint8_t sel = 0x01; sel <= 0x40; sel++) {
            if (g_nknown >= MAX_ENTRIES) return;

            uint8_t buf[MAX_LEN] = {0};
            uint8_t found_len = 0;

            // Try GET_LEN (bRequest=0x85) first
            uint8_t lbuf[2] = {0, 0};
            if (ctrl_get(iface, unit, sel, lbuf, 2, 0x85)) {
                uint16_t l = (uint16_t)lbuf[0] | ((uint16_t)lbuf[1] << 8);
                if (l > 0 && l <= MAX_LEN) {
                    if (ctrl_get(iface, unit, sel, buf, (uint8_t)l, 0x81))
                        found_len = (uint8_t)l;
                }
            }

            // Fallback: try common lengths
            if (!found_len) {
                for (int li = 0; li < (int)sizeof(TRY_LENS); li++) {
                    memset(buf, 0, sizeof(buf));
                    if (ctrl_get(iface, unit, sel, buf, TRY_LENS[li], 0x81)) {
                        found_len = TRY_LENS[li];
                        break;
                    }
                }
            }

            if (!found_len) continue;

            Entry *e = &g_known[g_nknown++];
            e->unit = unit;
            e->sel  = sel;
            e->len  = found_len;
            memcpy(e->data, buf, found_len);
        }
    }
}

// ── Snapshot helpers ──────────────────────────────────────────────────────────

static void refresh(IOUSBInterfaceInterface **iface, Entry *entries, int n)
{
    for (int i = 0; i < n; i++) {
        Entry *e = &entries[i];
        uint8_t buf[MAX_LEN] = {0};
        if (ctrl_get(iface, e->unit, e->sel, buf, e->len, 0x81))
            memcpy(e->data, buf, e->len);
    }
}

static void print_entries(Entry *entries, int n)
{
    for (int i = 0; i < n; i++) {
        Entry *e = &entries[i];
        printf("unit=%2d sel=0x%02x len=%d hex=", e->unit, e->sel, e->len);
        for (int j = 0; j < e->len; j++) printf("%02x", e->data[j]);
        printf("\n");
    }
    fflush(stdout);
}

// ── Watch mode ────────────────────────────────────────────────────────────────

static void print_ts(void)
{
    time_t t = time(NULL);
    struct tm *tm = localtime(&t);
    printf("[%02d:%02d:%02d] ", tm->tm_hour, tm->tm_min, tm->tm_sec);
}

static void watch(IOUSBInterfaceInterface **iface, int interval_ms)
{
    Entry cur[MAX_ENTRIES];
    memcpy(cur, g_known, g_nknown * sizeof(Entry));

    fprintf(stderr, "Watching %d selectors (interval %dms). "
            "Send link-ctl commands in another terminal.\n",
            g_nknown, interval_ms);

    for (;;) {
        Entry nxt[MAX_ENTRIES];
        memcpy(nxt, cur, g_nknown * sizeof(Entry));
        refresh(iface, nxt, g_nknown);

        for (int i = 0; i < g_nknown; i++) {
            if (memcmp(cur[i].data, nxt[i].data, cur[i].len) == 0) continue;
            print_ts();
            printf("CHANGED unit=%2d sel=0x%02x  ", cur[i].unit, cur[i].sel);
            for (int j = 0; j < cur[i].len; j++) printf("%02x", cur[i].data[j]);
            printf(" → ");
            for (int j = 0; j < nxt[i].len; j++) printf("%02x", nxt[i].data[j]);
            printf("\n");
            fflush(stdout);
        }
        memcpy(cur, nxt, g_nknown * sizeof(Entry));
        usleep(interval_ms * 1000);
    }
}

// ── Server mode (pipe) ────────────────────────────────────────────────────────
// Reads one line from stdin → takes snapshot → prints entries → prints "END\n"

static void server(IOUSBInterfaceInterface **iface)
{
    fprintf(stderr, "server: ready (%d selectors). "
            "Send newline to trigger snapshot.\n", g_nknown);
    fflush(stderr);

    char buf[64];
    while (fgets(buf, sizeof(buf), stdin)) {
        Entry snap[MAX_ENTRIES];
        memcpy(snap, g_known, g_nknown * sizeof(Entry));
        refresh(iface, snap, g_nknown);
        print_entries(snap, g_nknown);
        printf("END\n");
        fflush(stdout);
    }
}

// ── main ──────────────────────────────────────────────────────────────────────

int main(int argc, char *argv[])
{
    const char *mode = argc > 1 ? argv[1] : "snapshot";
    int interval_ms  = argc > 2 ? atoi(argv[2]) : 200;
    if (interval_ms < 50) interval_ms = 50;

    IOUSBInterfaceInterface **iface = find_vc_interface();
    if (!iface) { fputs("VideoControl interface not found\n", stderr); return 1; }

    IOReturn kr = (*iface)->USBInterfaceOpen(iface);
    if (kr != kIOReturnSuccess && kr != kIOReturnExclusiveAccess) {
        fprintf(stderr, "USBInterfaceOpen: %#x\n", kr); return 1;
    }
    if (kr == kIOReturnExclusiveAccess)
        fputs("(sharing interface with UVCAssistant)\n", stderr);

    // Commands that don't need discovery
    if (strcmp(mode, "get") == 0) {
        // get <unit> <sel> <len>
        if (argc < 5) { fprintf(stderr, "Usage: uvc-probe get <unit> <sel> <len>\n"); return 1; }
        uint8_t unit = (uint8_t)strtol(argv[2], NULL, 0);
        uint8_t sel  = (uint8_t)strtol(argv[3], NULL, 0);
        uint8_t len  = (uint8_t)strtol(argv[4], NULL, 0);
        uint8_t buf[MAX_LEN] = {0};
        if (ctrl_get(iface, unit, sel, buf, len, 0x81)) {
            for (int i = 0; i < len; i++) printf("%02x", buf[i]);
            printf("\n");
        } else {
            fprintf(stderr, "GET_CUR failed: unit=%d sel=0x%02x len=%d\n", unit, sel, len);
            return 1;
        }
    } else if (strcmp(mode, "set") == 0) {
        // set <unit> <sel> <hex_data>
        if (argc < 5) { fprintf(stderr, "Usage: uvc-probe set <unit> <sel> <hex>\n"); return 1; }
        uint8_t unit = (uint8_t)strtol(argv[2], NULL, 0);
        uint8_t sel  = (uint8_t)strtol(argv[3], NULL, 0);
        const char *hex = argv[4];
        uint8_t buf[MAX_LEN] = {0};
        size_t hlen = strlen(hex);
        uint8_t dlen = (uint8_t)(hlen / 2);
        for (int i = 0; i < dlen && i < MAX_LEN; i++) {
            unsigned int b; sscanf(hex + i*2, "%2x", &b); buf[i] = (uint8_t)b;
        }
        if (ctrl_set(iface, unit, sel, buf, dlen)) {
            printf("OK\n");
        } else {
            fprintf(stderr, "SET_CUR failed: unit=%d sel=0x%02x\n", unit, sel);
            return 1;
        }
    } else if (strcmp(mode, "getset") == 0) {
        // getset <unit> <sel> <hex_data>  — SET then GET back
        if (argc < 5) { fprintf(stderr, "Usage: uvc-probe getset <unit> <sel> <hex>\n"); return 1; }
        uint8_t unit = (uint8_t)strtol(argv[2], NULL, 0);
        uint8_t sel  = (uint8_t)strtol(argv[3], NULL, 0);
        const char *hex = argv[4];
        uint8_t buf[MAX_LEN] = {0};
        size_t hlen = strlen(hex);
        uint8_t dlen = (uint8_t)(hlen / 2);
        for (int i = 0; i < dlen && i < MAX_LEN; i++) {
            unsigned int b; sscanf(hex + i*2, "%2x", &b); buf[i] = (uint8_t)b;
        }
        if (!ctrl_set(iface, unit, sel, buf, dlen)) {
            fprintf(stderr, "SET_CUR failed: unit=%d sel=0x%02x\n", unit, sel);
            return 1;
        }
        printf("SET: ");
        for (int i = 0; i < dlen; i++) printf("%02x", buf[i]);
        printf("\n");
        usleep(200000);  // 200ms settle
        memset(buf, 0, sizeof(buf));
        if (ctrl_get(iface, unit, sel, buf, dlen, 0x81)) {
            printf("GET: ");
            for (int i = 0; i < dlen; i++) printf("%02x", buf[i]);
            printf("\n");
        } else {
            fprintf(stderr, "GET_CUR failed after SET\n");
        }
    } else {
        // Commands that need discovery
        fprintf(stderr, "Discovering selectors...\n");
        discover(iface);
        fprintf(stderr, "Found %d readable selectors.\n", g_nknown);

        if (strcmp(mode, "snapshot") == 0) {
            print_entries(g_known, g_nknown);
        } else if (strcmp(mode, "watch") == 0) {
            watch(iface, interval_ms);
        } else if (strcmp(mode, "server") == 0) {
            server(iface);
        } else {
            fprintf(stderr, "Usage: uvc-probe [snapshot|watch [ms]|server|get|set|getset]\n");
            return 1;
        }
    }

    (*iface)->USBInterfaceClose(iface);
    (*iface)->Release(iface);
    return 0;
}
