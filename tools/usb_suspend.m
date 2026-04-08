/*
 * usb_suspend.m — Suspend/resume a USB device by VID:PID on macOS
 *
 * Usage:
 *   sudo tools/usb-suspend suspend    # suspend the camera (LED off, stream stops)
 *   sudo tools/usb-suspend resume     # resume the camera
 *   sudo tools/usb-suspend status     # check if suspended
 *
 * Compile:
 *   clang -o tools/usb-suspend tools/usb_suspend.m \
 *       -framework IOKit -framework CoreFoundation -framework Foundation -ObjC
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#import  <Foundation/Foundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/usb/USBSpec.h>

#define INSTA360_VID  0x2E1A
#define INSTA360_PID  0x4C01

static IOUSBDeviceInterface **find_device(void)
{
    io_iterator_t iter = 0;
    NSMutableDictionary *match = (__bridge NSMutableDictionary *)IOServiceMatching("IOUSBHostDevice");
    [match setObject:@(INSTA360_VID) forKey:@"idVendor"];
    [match setObject:@(INSTA360_PID) forKey:@"idProduct"];
    IOServiceGetMatchingServices(kIOMainPortDefault, (__bridge CFDictionaryRef)match, &iter);

    io_service_t svc = IOIteratorNext(iter);
    IOObjectRelease(iter);
    if (!svc) return NULL;

    IOCFPlugInInterface **plugin = NULL;
    SInt32 score;
    IOCreatePlugInInterfaceForService(svc,
        kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID, &plugin, &score);
    IOObjectRelease(svc);
    if (!plugin) return NULL;

    IOUSBDeviceInterface **dev = NULL;
    (*plugin)->QueryInterface(plugin,
        CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID187), (LPVOID *)&dev);
    (*plugin)->Release(plugin);
    return dev;
}

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr, "Usage: usb-suspend [suspend|resume|status]\n");
        return 1;
    }
    const char *cmd = argv[1];

    IOUSBDeviceInterface **dev = find_device();
    if (!dev) {
        fprintf(stderr, "Insta360 Link not found (VID:PID %04x:%04x)\n",
                INSTA360_VID, INSTA360_PID);
        return 1;
    }
    fprintf(stderr, "Found Insta360 Link\n");

    if (strcmp(cmd, "status") == 0) {
        // Check device info without opening
        UInt32 info = 0;
        IOReturn kr = (*dev)->GetUSBDeviceInformation(dev, &info);
        if (kr == kIOReturnSuccess) {
            int suspended = (info >> 5) & 1;  // bit 5 = kUSBInformationDeviceIsSuspendedBit
            printf("suspended=%d (info=0x%08x)\n", suspended, info);
        } else {
            fprintf(stderr, "GetUSBDeviceInformation: %#x\n", kr);
        }
        (*dev)->Release(dev);
        return 0;
    }

    // Need to open the device for suspend/resume
    // Try USBDeviceOpenSeize first (asks current owner to yield)
    IOReturn kr = (*dev)->USBDeviceOpenSeize(dev);
    if (kr == kIOReturnSuccess) {
        fprintf(stderr, "Device opened (seized)\n");
    } else if (kr == kIOReturnExclusiveAccess) {
        fprintf(stderr, "USBDeviceOpenSeize: exclusive access denied (UVCAssistant won't yield)\n");
        // Try plain open
        kr = (*dev)->USBDeviceOpen(dev);
        if (kr == kIOReturnSuccess) {
            fprintf(stderr, "Device opened (plain)\n");
        } else {
            fprintf(stderr, "USBDeviceOpen: %#x — trying suspend anyway...\n", kr);
        }
    } else {
        fprintf(stderr, "USBDeviceOpenSeize: %#x\n", kr);
    }

    if (strcmp(cmd, "suspend") == 0) {
        fprintf(stderr, "Suspending device...\n");
        kr = (*dev)->USBDeviceSuspend(dev, 1);
        if (kr == kIOReturnSuccess) {
            printf("OK — device suspended\n");
        } else {
            fprintf(stderr, "USBDeviceSuspend: %#x\n", kr);
            (*dev)->USBDeviceClose(dev);
            (*dev)->Release(dev);
            return 1;
        }
    } else if (strcmp(cmd, "resume") == 0) {
        // Resume + re-enumerate to force full device reinitialization.
        // USBDeviceReEnumerate simulates unplug/replug at the USB level,
        // causing the camera firmware to fully restart and UVCAssistant
        // to re-discover the device.
        fprintf(stderr, "Re-enumerating device (simulates unplug/replug)...\n");
        kr = (*dev)->USBDeviceReEnumerate(dev, 0);
        if (kr == kIOReturnSuccess) {
            printf("OK — device re-enumerated\n");
        } else {
            fprintf(stderr, "USBDeviceReEnumerate: %#x\n", kr);
            // Fallback: try plain resume
            fprintf(stderr, "Falling back to plain resume...\n");
            kr = (*dev)->USBDeviceSuspend(dev, 0);
            if (kr == kIOReturnSuccess) {
                printf("OK — device resumed (plain)\n");
            } else {
                fprintf(stderr, "USBDeviceSuspend(resume): %#x\n", kr);
                (*dev)->USBDeviceClose(dev);
                (*dev)->Release(dev);
                return 1;
            }
        }
    }

    // Try to close if we opened it
    (*dev)->USBDeviceClose(dev);
    (*dev)->Release(dev);
    return 0;
}
