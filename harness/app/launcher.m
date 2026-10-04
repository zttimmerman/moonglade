// Hearthstone.app launcher (openspec/changes/launcher-app, task 2.2). Built into Contents/MacOS/Hearthstone by
// `harness/hs app` (harness/hslib/app.py); see that module and design.md.
//
// What it does: reads the repo's absolute path from Contents/Resources/repo-path (written at build time, so a copy of
// the app anywhere still finds the repo), runs `<repo>/play` (the same play loop as in a terminal) with its output
// appended to <repo>/env/logs/app.log, shows a small "starting" window until the game client launches, and exits when
// the play loop does: 0 after a normal game exit; nonzero with an error dialog (problem + log path) when something
// failed. If the play loop's single-instance lock is held (another ./play or app instance), it shows "already
// running" and exits 0. Opening the app again while it runs (LaunchServices sends the running instance a reopen
// event) shows the same message. Quitting the app (Dock menu, Cmd-Q, SIGTERM) asks the play loop to stop (SIGTERM),
// which closes the game and shuts the server down, then exits.
//
// Test hook: with HS_APP_TEST_HOOK=<file> in the environment, no window or dialog is shown; every status/dialog the
// app would show, and its exit code, is appended to <file> as one JSON object per line. The harness's app.* checks
// always set it (via `open --env`), so harness runs never pop up real dialogs.
#import <Cocoa/Cocoa.h>
#include <signal.h>
#include <fcntl.h>
#include <unistd.h>

static NSString *const kHookEnv = @"HS_APP_TEST_HOOK";

@interface HSLauncher : NSObject <NSApplicationDelegate>
@property(strong) NSString *repo;
@property(strong) NSString *logPath;
@property(strong) NSString *hookPath;
@property(strong) NSTask *task;
@property(strong) NSFileHandle *logHandle;
@property(strong) NSWindow *statusWindow;
@property(strong) NSTextField *statusDetail;
@property(strong) NSTimer *tailTimer;
@property(strong) NSMutableArray<NSString *> *playLines;   // the play loop's "[play] ..." messages
@property(strong) NSMutableArray<NSString *> *outLines;    // every other line it (or bash) printed
@property(strong) NSMutableString *partial;
@property(strong) NSMutableArray *signalSources;
@property unsigned long long tailOffset;
@property BOOL stopRequested;
@property BOOL clientStarted;
@end

@implementation HSLauncher

- (BOOL)testMode { return self.hookPath.length > 0; }

- (NSString *)now {
    NSISO8601DateFormatter *f = [NSISO8601DateFormatter new];
    f.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
    return [f stringFromDate:[NSDate date]];
}

- (void)appLog:(NSString *)line {
    if (!self.logHandle) return;
    NSString *s = [NSString stringWithFormat:@"[app %@] %@\n", [self now], line];
    @try { [self.logHandle writeData:[s dataUsingEncoding:NSUTF8StringEncoding]]; } @catch (NSException *e) {}
}

// One event: always logged to app.log; in test mode appended to the hook file instead of being shown.
- (void)record:(NSDictionary *)event {
    NSMutableDictionary *ev = [event mutableCopy];
    ev[@"t"] = [self now];
    ev[@"pid"] = @(getpid());
    ev[@"bundle"] = [[NSBundle mainBundle] bundlePath];
    NSData *json = [NSJSONSerialization dataWithJSONObject:ev options:NSJSONWritingSortedKeys error:nil];
    NSString *js = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
    [self appLog:[@"event " stringByAppendingString:js]];
    if ([self testMode]) {
        int fd = open(self.hookPath.fileSystemRepresentation, O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (fd >= 0) {
            NSMutableData *d = [json mutableCopy];
            [d appendBytes:"\n" length:1];
            write(fd, d.bytes, d.length);
            close(fd);
        }
    }
}

- (void)finish:(int)code {
    [self record:@{@"kind": @"exit", @"code": @(code)}];
    [self appLog:[NSString stringWithFormat:@"exit %d", code]];
    exit(code);
}

// ------------------------------------------------------------------ UI (suppressed in test mode)

- (void)showStatus:(NSString *)ident text:(NSString *)text {
    [self record:@{@"kind": @"status", @"id": ident, @"text": text}];
    if ([self testMode]) return;
    if (!self.statusWindow) {
        NSRect r = NSMakeRect(0, 0, 440, 120);
        NSWindow *w = [[NSWindow alloc] initWithContentRect:r
                                                  styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskMiniaturizable
                                                    backing:NSBackingStoreBuffered defer:NO];
        w.title = @"Hearthstone";
        w.releasedWhenClosed = NO;
        NSImageView *iv = [NSImageView imageViewWithImage:[NSApp applicationIconImage]];
        iv.frame = NSMakeRect(20, 28, 64, 64);
        [w.contentView addSubview:iv];
        NSTextField *title = [NSTextField labelWithString:text];
        title.font = [NSFont boldSystemFontOfSize:14];
        title.frame = NSMakeRect(100, 70, 320, 22);
        title.tag = 1;
        [w.contentView addSubview:title];
        NSTextField *detail = [NSTextField wrappingLabelWithString:@"Starting the local server. This takes a minute or two."];
        detail.font = [NSFont systemFontOfSize:11];
        detail.textColor = [NSColor secondaryLabelColor];
        detail.frame = NSMakeRect(100, 20, 320, 44);
        [w.contentView addSubview:detail];
        NSProgressIndicator *spin = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(400, 72, 18, 18)];
        spin.style = NSProgressIndicatorStyleSpinning;
        spin.controlSize = NSControlSizeSmall;
        [spin startAnimation:nil];
        [w.contentView addSubview:spin];
        self.statusDetail = detail;
        self.statusWindow = w;
        [w center];
    }
    [(NSTextField *)[self.statusWindow.contentView viewWithTag:1] setStringValue:text];
    [NSApp activateIgnoringOtherApps:YES];
    [self.statusWindow makeKeyAndOrderFront:nil];
}

- (void)hideStatus {
    [self.statusWindow orderOut:nil];
}

- (void)dialog:(NSString *)ident title:(NSString *)title text:(NSString *)text style:(NSAlertStyle)style showLog:(BOOL)showLog {
    [self record:@{@"kind": @"dialog", @"id": ident, @"title": title, @"text": text}];
    if ([self testMode]) return;
    [self hideStatus];
    NSAlert *a = [NSAlert new];
    a.alertStyle = style;
    a.messageText = title;
    a.informativeText = text;
    [a addButtonWithTitle:@"OK"];
    if (showLog && self.logPath) [a addButtonWithTitle:@"Show Log"];
    [NSApp activateIgnoringOtherApps:YES];
    if ([a runModal] == NSAlertSecondButtonReturn && self.logPath)
        [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:self.logPath]];
}

// ------------------------------------------------------------------ startup

- (NSString *)resource:(NSString *)name {
    NSString *p = [[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:name];
    NSString *s = [NSString stringWithContentsOfFile:p encoding:NSUTF8StringEncoding error:nil];
    return [s stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
}

- (void)applicationDidFinishLaunching:(NSNotification *)n {
    self.hookPath = NSProcessInfo.processInfo.environment[kHookEnv];
    self.playLines = [NSMutableArray array];
    self.outLines = [NSMutableArray array];
    self.partial = [NSMutableString string];
    [self installSignalHandlers];

    NSString *repo = [self resource:@"repo-path"];
    NSString *play = [repo stringByAppendingPathComponent:@"play"];
    if (repo.length == 0 || ![[NSFileManager defaultManager] isExecutableFileAtPath:play]) {
        [self dialog:@"repo_missing" title:@"Hearthstone can't find its folder"
                text:[NSString stringWithFormat:@"This app runs the game from the Hearthstone folder at\n%@\nbut that "
                      @"folder (or its `play` script) isn't there any more. If you moved the folder, rebuild the app "
                      @"from it with `harness/hs app`.\n\nNo log was written.", repo.length ? repo : @"(not recorded)"]
               style:NSAlertStyleCritical showLog:NO];
        [self finish:1];
    }
    self.repo = repo;
    NSString *logs = [repo stringByAppendingPathComponent:@"env/logs"];
    [[NSFileManager defaultManager] createDirectoryAtPath:logs withIntermediateDirectories:YES attributes:nil error:nil];
    self.logPath = [logs stringByAppendingPathComponent:@"app.log"];
    int fd = open(self.logPath.fileSystemRepresentation, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) self.logHandle = [[NSFileHandle alloc] initWithFileDescriptor:fd closeOnDealloc:YES];
    [self appLog:[NSString stringWithFormat:@"==== Hearthstone.app pid %d from %@ (repo %@)", getpid(),
                  [[NSBundle mainBundle] bundlePath], repo]];

    NSMutableArray *args = [NSMutableArray arrayWithObject:play];
    NSArray *argv = NSProcessInfo.processInfo.arguments;
    for (NSUInteger i = 1; i < argv.count; i++)
        if (![argv[i] hasPrefix:@"-psn_"]) [args addObject:argv[i]];

    NSMutableDictionary *env = [NSProcessInfo.processInfo.environment mutableCopy];
    NSString *pyDir = [self resource:@"python-dir"];
    NSString *path = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
    env[@"PATH"] = pyDir.length ? [NSString stringWithFormat:@"%@:%@", pyDir, path] : path;
    [env removeObjectForKey:kHookEnv];                 // the hook is the app's, not the play loop's
    env[@"PYTHONUNBUFFERED"] = @"1";
    env[@"HS_FROM_APP"] = @"1";                        // ./play then keeps passwords out of app.log (play.shown_password)

    unsigned long long size = [[[NSFileManager defaultManager] attributesOfItemAtPath:self.logPath error:nil] fileSize];
    self.tailOffset = size;
    NSTask *t = [NSTask new];
    t.executableURL = [NSURL fileURLWithPath:@"/bin/bash"];
    t.arguments = args;
    t.currentDirectoryURL = [NSURL fileURLWithPath:repo];
    t.environment = env;
    t.standardInput = [NSFileHandle fileHandleWithNullDevice];
    t.standardOutput = self.logHandle ?: [NSFileHandle fileHandleWithNullDevice];
    t.standardError = self.logHandle ?: [NSFileHandle fileHandleWithNullDevice];
    __weak HSLauncher *weakSelf = self;
    t.terminationHandler = ^(NSTask *task) {
        dispatch_async(dispatch_get_main_queue(), ^{ [weakSelf playExited:task]; });
    };
    [self showStatus:@"starting" text:@"Hearthstone is starting…"];
    NSError *err = nil;
    if (![t launchAndReturnError:&err]) {
        [self dialog:@"error" title:@"Hearthstone couldn't start"
                text:[NSString stringWithFormat:@"The play loop couldn't be started: %@\n\nLog: %@",
                      err.localizedDescription, self.logPath] style:NSAlertStyleCritical showLog:YES];
        [self finish:1];
    }
    self.task = t;
    [self appLog:[NSString stringWithFormat:@"play loop pid %d: %@", t.processIdentifier, [args componentsJoinedByString:@" "]]];
    self.tailTimer = [NSTimer scheduledTimerWithTimeInterval:0.5 target:self selector:@selector(tail)
                                                    userInfo:nil repeats:YES];
}

// Follow the play loop's "[play] ..." lines in app.log: the latest one is the status window's detail text; the
// "game is starting" line means the client has been launched, so the status window goes away.
- (void)tail {
    NSFileHandle *h = [NSFileHandle fileHandleForReadingAtPath:self.logPath];
    if (!h) return;
    [h seekToFileOffset:self.tailOffset];
    NSData *d = [h readDataToEndOfFile];
    [h closeFile];
    if (d.length == 0) return;
    self.tailOffset += d.length;
    [self.partial appendString:[[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] ?: @""];
    NSArray *parts = [self.partial componentsSeparatedByString:@"\n"];
    [self.partial setString:parts.lastObject];
    for (NSUInteger i = 0; i + 1 < parts.count; i++) {
        NSString *l = parts[i];
        if ([l hasPrefix:@"[app "]) continue;
        if (![l hasPrefix:@"[play] "]) {
            NSString *t = [l stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceCharacterSet]];
            if (t.length) [self.outLines addObject:t.length > 300 ? [t substringToIndex:300] : t];
            if (self.outLines.count > 50) [self.outLines removeObjectAtIndex:0];
            continue;
        }
        NSString *msg = [l substringFromIndex:7];
        [self.playLines addObject:msg];
        if (self.playLines.count > 50) [self.playLines removeObjectAtIndex:0];
        if (!self.clientStarted && !self.stopRequested) self.statusDetail.stringValue = msg;
        if (!self.clientStarted && [msg hasPrefix:@"The game is starting"]) {
            self.clientStarted = YES;
            [self record:@{@"kind": @"status", @"id": @"client_launched", @"text": msg}];
            [self hideStatus];
        }
    }
}

// ------------------------------------------------------------------ the play loop ended

- (NSDictionary *)playRecord:(int)pid {
    NSString *p = [self.repo stringByAppendingPathComponent:@"env/run/play-last.json"];
    NSData *d = [NSData dataWithContentsOfFile:p];
    NSDictionary *rec = d ? [NSJSONSerialization JSONObjectWithData:d options:0 error:nil] : nil;
    if (![rec isKindOfClass:[NSDictionary class]] || [rec[@"pid"] intValue] != pid) return nil;
    return rec;
}

- (NSString *)problemFrom:(NSDictionary *)rec code:(int)code {
    NSMutableArray *parts = [NSMutableArray array];
    NSString *err = [rec[@"error"] isKindOfClass:[NSString class]] ? rec[@"error"] : nil;
    NSString *crash = [rec[@"crash_message"] isKindOfClass:[NSString class]] ? rec[@"crash_message"] : nil;
    if (crash) [parts addObject:[NSString stringWithFormat:@"The game %@.", [crash stringByReplacingOccurrencesOfString:@"client crash: the client " withString:@"client "]]];
    if (err) [parts addObject:err];
    NSString *excerpt = [rec[@"log_excerpt"] isKindOfClass:[NSString class]] ? rec[@"log_excerpt"] : nil;
    if (excerpt) {                                      // the server's own words for why it stopped
        NSMutableArray *hits = [NSMutableArray array];
        for (NSString *l in [excerpt componentsSeparatedByString:@"\n"]) {
            NSString *s = [l stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceCharacterSet]];
            if (s.length && ([s rangeOfString:@"error" options:NSCaseInsensitiveSearch].location != NSNotFound ||
                             [s rangeOfString:@"fail" options:NSCaseInsensitiveSearch].location != NSNotFound))
                [hits addObject:s.length > 200 ? [s substringToIndex:200] : s];
        }
        if (hits.count) {
            NSRange r = NSMakeRange(hits.count > 3 ? hits.count - 3 : 0, MIN(hits.count, 3));
            [parts addObject:[@"Server log: " stringByAppendingString:[[hits subarrayWithRange:r] componentsJoinedByString:@"\n"]]];
        }
    }
    NSArray *remaining = [rec[@"cleanup"] isKindOfClass:[NSDictionary class]] ? rec[@"cleanup"][@"remaining"] : nil;
    if ([remaining isKindOfClass:[NSArray class]] && remaining.count)
        [parts addObject:[NSString stringWithFormat:@"%lu part(s) were still running after shutdown.", (unsigned long)remaining.count]];
    if (parts.count == 0) {
        NSArray *lines = self.outLines.count ? self.outLines : self.playLines;   // stray output is usually the error
        NSRange r = NSMakeRange(lines.count > 3 ? lines.count - 3 : 0, MIN(lines.count, (NSUInteger)3));
        NSString *last = [[lines subarrayWithRange:r] componentsJoinedByString:@"\n"];
        [parts addObject:last.length ? last : [NSString stringWithFormat:@"The play loop exited with code %d.", code]];
    }
    for (NSString *l in [self.outLines arrayByAddingObjectsFromArray:self.playLines])
        if ([l rangeOfString:@"Operation not permitted"].location != NSNotFound) {
            [parts addObject:@"macOS blocked access to the Hearthstone folder. Allow it in System Settings > Privacy & "
                             @"Security > Files and Folders > Hearthstone (Documents Folder), then open the app again."];
            break;
        }
    return [parts componentsJoinedByString:@"\n\n"];
}

- (void)playExited:(NSTask *)task {
    [self.tailTimer invalidate];
    [self tail];
    int code = task.terminationStatus;
    if (task.terminationReason == NSTaskTerminationReasonUncaughtSignal) code = 128 + code;
    [self appLog:[NSString stringWithFormat:@"play loop exited with %d", code]];
    [self hideStatus];
    if (code == 0) [self finish:0];
    if (code == 2) {                                    // play.py EXIT_BUSY: the single-instance lock is held
        [self dialog:@"already_running" title:@"Hearthstone is already running"
                text:@"The game is already open (or still starting). Use that window, or quit the game first."
               style:NSAlertStyleInformational showLog:NO];
        [self finish:0];
    }
    NSDictionary *rec = [self playRecord:task.processIdentifier];
    if (self.stopRequested && code >= 128 && ![[rec[@"cleanup"] objectForKey:@"remaining"] count]) [self finish:0];
    BOOL started = [rec[@"client_started"] boolValue] || self.clientStarted;
    NSString *title = started ? @"Hearthstone stopped because of a problem" : @"Hearthstone couldn't start";
    NSString *text = [NSString stringWithFormat:@"%@\n\nThe full log is at:\n%@", [self problemFrom:rec code:code], self.logPath];
    [self dialog:@"error" title:title text:text style:NSAlertStyleCritical showLog:YES];
    [self finish:(code > 0 && code < 256) ? code : 1];
}

// ------------------------------------------------------------------ reopen, quit, signals

- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)flag {
    if (self.task.running) {
        [self dialog:@"already_running" title:@"Hearthstone is already running"
                text:(self.clientStarted ? @"The game is already open. Quit the game to stop everything."
                                         : @"Hearthstone is still starting. The game will open by itself.")
               style:NSAlertStyleInformational showLog:NO];
        if (!self.clientStarted && ![self testMode]) [self.statusWindow makeKeyAndOrderFront:nil];
    }
    return NO;
}

- (void)requestStop:(NSString *)why {
    if (!self.task.running) return;
    if (!self.stopRequested) {
        self.stopRequested = YES;
        [self appLog:[NSString stringWithFormat:@"stop requested (%@): sending SIGTERM to the play loop", why]];
        [self record:@{@"kind": @"status", @"id": @"stopping", @"text": @"Hearthstone is shutting down…"}];
        if (!self.clientStarted && ![self testMode]) self.statusDetail.stringValue = @"Shutting down…";
        [self.task terminate];
    }
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    if (self.task.running) {                            // stop the play loop first; playExited: exits the app
        [self requestStop:@"quit"];
        return NSTerminateCancel;
    }
    return NSTerminateNow;
}

- (void)installSignalHandlers {
    self.signalSources = [NSMutableArray array];
    int sigs[] = {SIGTERM, SIGINT, SIGHUP};
    for (int k = 0; k < 3; k++) {
        int sig = sigs[k];
        signal(sig, SIG_IGN);
        dispatch_source_t s = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, sig, 0, dispatch_get_main_queue());
        __weak HSLauncher *weakSelf = self;
        dispatch_source_set_event_handler(s, ^{
            HSLauncher *me = weakSelf;
            if (me.task.running) [me requestStop:[NSString stringWithFormat:@"signal %d", sig]];
            else [me finish:128 + sig];
        });
        dispatch_resume(s);
        [self.signalSources addObject:s];
    }
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        HSLauncher *d = [HSLauncher new];
        app.delegate = d;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app run];
    }
    return 0;
}
