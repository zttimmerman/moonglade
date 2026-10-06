// Moonglade Wine host shim (ADR-0010, openspec/changes/launcher-polish task 2.1). Built by `harness/hs app`
// (harness/hslib/dockshim.py) into env/dockshim/hsdock.dylib, universal x86_64 (the Wine host runs under Rosetta)
// + arm64. Loaded only into the game client's Wine processes, through DYLD_INSERT_LIBRARIES set by
// launch.wine_env(client=True) (never globally; bash would strip it anyway). It changes nothing in Wow.exe, the
// Windows side, rendering or client/: it only adjusts how the macOS host process presents itself.
//
// Environment (set by launch.dock_env):
//   HSDOCK_POLICY  accessory = no Dock icon of its own (app runs: the launcher is the one Moonglade icon);
//                  regular   = keep Wine's normal Dock icon (terminal ./play, or the option-B fallback)
//   HSDOCK_NAME    the display name (Dock, Cmd-Tab, Force Quit) instead of "wine"
//   HSDOCK_ICON    an .icns that replaces the exe icon Wine passes to -[NSApplication setApplicationIconImage:]
//   HSDOCK_LOG     log file (env/logs/dockshim.log); one line per action, with the pid, for the checks
//   HSDOCK_TOKEN   this repo's path: the object of our distributed notifications, so only this repo's game reacts
//   HSDOCK_TEST    1 = test-only hooks (only with ./play's --client-cmd stand-in): the "minimise" notification and
//                  HSDOCK_TEST_FLICKER_MS (let Wine's Regular through, switch to accessory N ms later: a deliberate
//                  Dock-icon flash, the counterexample for app.single_dock_icon)
//
// What it does, once AppKit and Wine's Mac driver are loaded (nothing before; non-GUI Wine processes are untouched):
//   - -[NSApplication setActivationPolicy:]: Regular -> Accessory when HSDOCK_POLICY=accessory; on the first call
//     (a window is about to appear) also sets the display name and starts listening for our notifications.
//   - -[NSApplication setApplicationIconImage:]: our icon instead of the exe's.
//   - -[WineApplicationController transformProcessToForeground:]: runs once. Wine calls it every time a window is
//     ordered in while the policy isn't Regular, which under Accessory would rebuild menus and re-request
//     activation each time (spike Q-A risks). After the first run it posts local.moonglade.window (pid) so the
//     launcher can hand focus to the game at once.
//   - local.moonglade.reopen (posted by the launcher on a Dock/Finder click or Cmd-Tab): calls Wine's own reopen
//     handler, -[WineApplicationController applicationShouldHandleReopen:hasVisibleWindows:], which unminimises
//     the game window (cocoa_app.m unminimizeWindowIfNoneVisible).
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <stdatomic.h>
#include <unistd.h>

static NSString *const kReopen = @"local.moonglade.reopen";
static NSString *const kWindow = @"local.moonglade.window";
static NSString *const kTestMinimize = @"local.moonglade.test.minimize";

static IMP orig_setPolicy, orig_setIcon, orig_transform;
static atomic_int swizzledApp, swizzledWine, transformRuns, policyCalls;
static id iconImage;

static const char *env(const char *k) { const char *v = getenv(k); return (v && *v) ? v : NULL; }
static BOOL envIs(const char *k, const char *want) { const char *v = env(k); return v && strcmp(v, want) == 0; }
static BOOL testMode(void) { return envIs("HSDOCK_TEST", "1"); }

static void hslog(NSString *fmt, ...) NS_FORMAT_FUNCTION(1, 2);
static void hslog(NSString *fmt, ...) {
    const char *path = env("HSDOCK_LOG");
    if (!path) return;
    va_list ap;
    va_start(ap, fmt);
    NSString *msg = [[NSString alloc] initWithFormat:fmt arguments:ap];
    va_end(ap);
    NSDateFormatter *f = [NSDateFormatter new];
    f.locale = [NSLocale localeWithLocaleIdentifier:@"en_US_POSIX"];
    f.dateFormat = @"yyyy-MM-dd'T'HH:mm:ss.SSSZZZZZ";
    NSString *line = [NSString stringWithFormat:@"%@ pid=%d policy=%s %@\n", [f stringFromDate:[NSDate date]], getpid(),
                      env("HSDOCK_POLICY") ?: "-", msg];
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) {
        NSData *d = [line dataUsingEncoding:NSUTF8StringEncoding];
        write(fd, d.bytes, d.length);
        close(fd);
    }
}

// ------------------------------------------------------------------ display name (private LaunchServices API)

static void setDisplayName(void) {
    const char *name = env("HSDOCK_NAME");
    if (!name) return;
    void *ls = dlopen("/System/Library/Frameworks/CoreServices.framework/CoreServices", RTLD_LAZY);
    typedef CFTypeRef (*GetASN)(void);
    typedef OSStatus (*SetItem)(int, CFTypeRef, CFStringRef, CFTypeRef, CFDictionaryRef *);
    GetASN getASN = ls ? (GetASN)dlsym(ls, "_LSGetCurrentApplicationASN") : NULL;
    SetItem setItem = ls ? (SetItem)dlsym(ls, "_LSSetApplicationInformationItem") : NULL;
    CFStringRef *key = ls ? (CFStringRef *)dlsym(ls, "_kLSDisplayNameKey") : NULL;
    if (!getASN || !setItem || !key) {
        hslog(@"display name: LaunchServices symbols missing (asn=%p set=%p key=%p)", getASN, setItem, key);
        return;
    }
    CFTypeRef asn = getASN();
    OSStatus st = asn ? setItem(-2, asn, *key, (__bridge CFStringRef)@(name), NULL) : -1;
    hslog(@"set display name '%s' status=%d", name, (int)st);
}

// ------------------------------------------------------------------ notifications from the launcher

static void unminimize(NSString *why) {
    Class appClass = objc_getClass("NSApplication");
    id app = ((id (*)(id, SEL))objc_msgSend)((id)appClass, sel_registerName("sharedApplication"));
    id delegate = ((id (*)(id, SEL))objc_msgSend)(app, sel_registerName("delegate"));
    NSArray *wins = ((id (*)(id, SEL))objc_msgSend)(app, sel_registerName("windows"));
    NSUInteger mini = 0;
    for (id w in wins) if (((BOOL (*)(id, SEL))objc_msgSend)(w, sel_registerName("isMiniaturized"))) mini++;
    SEL reopen = sel_registerName("applicationShouldHandleReopen:hasVisibleWindows:");
    BOOL called = NO;
    if (delegate && [delegate respondsToSelector:reopen]) {
        ((BOOL (*)(id, SEL, id, BOOL))objc_msgSend)(delegate, reopen, app, NO);
        called = YES;
    }
    hslog(@"reopen (%@): miniaturized windows before=%lu, Wine reopen handler called=%d", why, (unsigned long)mini, called);
}

static void minimizeForTest(void) {
    Class appClass = objc_getClass("NSApplication");
    id app = ((id (*)(id, SEL))objc_msgSend)((id)appClass, sel_registerName("sharedApplication"));
    NSArray *wins = ((id (*)(id, SEL))objc_msgSend)(app, sel_registerName("windows"));
    NSUInteger n = 0;
    for (id w in wins)
        if (((BOOL (*)(id, SEL))objc_msgSend)(w, sel_registerName("isVisible"))) {
            ((void (*)(id, SEL, id))objc_msgSend)(w, sel_registerName("miniaturize:"), nil);
            n++;
        }
    hslog(@"test: minimise requested, miniaturize: sent to %lu visible window(s)", (unsigned long)n);
}

@interface HSDockObserver : NSObject
@end
@implementation HSDockObserver
- (void)reopen:(NSNotification *)n { unminimize(n.name); }
- (void)minimize:(NSNotification *)n { minimizeForTest(); }
@end
static HSDockObserver *observer;

static void startObserving(void) {
    if (observer) return;
    observer = [HSDockObserver new];
    NSString *token = env("HSDOCK_TOKEN") ? @(env("HSDOCK_TOKEN")) : nil;
    NSDistributedNotificationCenter *dc = [NSDistributedNotificationCenter defaultCenter];
    // DeliverImmediately: an accessory app is never the active app, and AppKit suspends distributed notification
    // delivery for inactive apps by default
    [dc addObserver:observer selector:@selector(reopen:) name:kReopen object:token
        suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
    if (testMode())
        [dc addObserver:observer selector:@selector(minimize:) name:kTestMinimize object:token
            suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
    hslog(@"listening for %@%s (token %@)", kReopen, testMode() ? " and the test minimise notification" : "", token);
}

// ------------------------------------------------------------------ swizzles

static BOOL hs_setPolicy(id self, SEL _cmd, long policy) {
    int call = atomic_fetch_add(&policyCalls, 1) + 1;
    BOOL accessory = envIs("HSDOCK_POLICY", "accessory");
    long want = (policy == 0 /*Regular*/ && accessory) ? 1 /*Accessory*/ : policy;
    long flickerMs = (testMode() && env("HSDOCK_TEST_FLICKER_MS")) ? atol(env("HSDOCK_TEST_FLICKER_MS")) : 0;
    if (accessory && flickerMs > 0 && call == 1) want = policy;      // test-only: deliberate flash
    BOOL r = ((BOOL (*)(id, SEL, long))orig_setPolicy)(self, _cmd, want);
    hslog(@"setActivationPolicy:%ld -> using %ld (call %d) = %d", policy, want, call, r);
    if (call == 1) {
        setDisplayName();
        startObserving();
        if (accessory && flickerMs > 0) {
            hslog(@"test: flicker, switching to accessory in %ld ms", flickerMs);
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, flickerMs * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
                BOOL r2 = ((BOOL (*)(id, SEL, long))orig_setPolicy)(self, _cmd, 1);
                hslog(@"test: flicker over, setActivationPolicy:1 = %d", r2);
            });
        }
    }
    return r;
}

static void hs_setIcon(id self, SEL _cmd, id image) {
    const char *path = env("HSDOCK_ICON");
    if (path && !iconImage) {
        Class imgClass = objc_getClass("NSImage");
        iconImage = ((id (*)(id, SEL, id))objc_msgSend)(((id (*)(id, SEL))objc_msgSend)((id)imgClass, sel_registerName("alloc")),
                                                         sel_registerName("initWithContentsOfFile:"), @(path));
        if (!iconImage) hslog(@"icon: couldn't load %s", path);
    }
    id use = iconImage ?: image;
    ((void (*)(id, SEL, id))orig_setIcon)(self, _cmd, use);
    hslog(@"setApplicationIconImage: exe icon %s (replaced=%d)", image ? "given" : "nil", use != image);
}

static void hs_transform(id self, SEL _cmd, BOOL activate) {
    int run = atomic_fetch_add(&transformRuns, 1) + 1;
    if (run > 1) {                         // idempotent: once is all a single game window needs
        hslog(@"transformProcessToForeground: call %d skipped (already done)", run);
        return;
    }
    ((void (*)(id, SEL, BOOL))orig_transform)(self, _cmd, activate);
    hslog(@"transformProcessToForeground:%d ran (call 1)", activate);
    NSString *token = env("HSDOCK_TOKEN") ? @(env("HSDOCK_TOKEN")) : nil;
    int pid = getpid();
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 150 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [[NSDistributedNotificationCenter defaultCenter] postNotificationName:kWindow object:token
                                                                     userInfo:@{@"pid": @(pid)}
                                                                      options:NSDistributedNotificationDeliverImmediately];
        hslog(@"posted %@ (pid %d)", kWindow, pid);
    });
}

static IMP swap(Class c, const char *sel, IMP repl) {
    Method m = class_getInstanceMethod(c, sel_registerName(sel));
    return m ? method_setImplementation(m, repl) : NULL;
}

static void on_add_image(const struct mach_header *mh, intptr_t slide) {
    if (!atomic_load(&swizzledApp)) {
        Class app = objc_getClass("NSApplication");
        if (app && atomic_exchange(&swizzledApp, 1) == 0) {
            orig_setPolicy = swap(app, "setActivationPolicy:", (IMP)hs_setPolicy);
            orig_setIcon = swap(app, "setApplicationIconImage:", (IMP)hs_setIcon);
            hslog(@"AppKit loaded: hooks setActivationPolicy:%s setApplicationIconImage:%s (name=%s icon=%s test=%d)",
                  orig_setPolicy ? "ok" : "MISSING", orig_setIcon ? "ok" : "MISSING", env("HSDOCK_NAME") ?: "-",
                  env("HSDOCK_ICON") ?: "-", testMode());
        }
    }
    if (!atomic_load(&swizzledWine)) {
        Class wine = objc_getClass("WineApplicationController");
        if (wine && atomic_exchange(&swizzledWine, 1) == 0) {
            orig_transform = swap(wine, "transformProcessToForeground:", (IMP)hs_transform);
            hslog(@"Wine Mac driver loaded: hook transformProcessToForeground:%s", orig_transform ? "ok" : "MISSING");
        }
    }
}

__attribute__((constructor)) static void hsdock_init(void) {
    if (!env("HSDOCK_POLICY")) return;     // loaded without our settings: do nothing
    _dyld_register_func_for_add_image(on_add_image);
}
