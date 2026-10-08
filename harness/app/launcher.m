// Moonglade.app launcher (openspec/changes/launcher-app, task 2.2). Built into Contents/MacOS/Moonglade by
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
// Test hook: with HS_APP_TEST_HOOK=<file> in the environment, no dialog is shown (launcher-polish: the non-modal loading
// window still is, so activation behaves as in real use); every status/dialog the
// app would show, and its exit code, is appended to <file> as one JSON object per line. The harness's app.* checks
// always set it (via `open --env`), so harness runs never pop up real dialogs.
//
// launcher-polish (openspec/changes/launcher-polish, ADR-0010):
// - Loading window with stages: the play loop prints `[stage] {json}` lines (harness/hslib/stages.py); the window
//   shows the stage label, a determinate bar for the whole start (never moving backward; it creeps within a stage
//   that has no measurable progress), and the latest [play] line (the bots stage's heading is "AI players: N / M online"). Each stage line is
//   recorded as a hook event {"kind":"stage", ...} (play's own timestamp as "play_t").
// - One Dock icon: the game's Wine process runs as an accessory app (the shim, harness/app/dockshim.m), so this
//   launcher stays the only Moonglade icon. When the game's first window appears (the shim posts
//   local.moonglade.window; the play loop's `ready` stage is the fallback) the loading window goes away and
//   activation is handed to the game (yieldActivationToApplication: + activateFromApplication:). From then on a
//   Dock/Finder click (reopen) or activating the launcher (Cmd-Tab) posts local.moonglade.reopen (the shim
//   unminimises the game window) and hands activation to the game again, instead of "already running".
// - Test-only, honoured only with the test hook: HS_APP_TEST_DISABLE=handoff,forward,reopen_note,early_hide (counterexamples
//   that switch one of those off) and HS_APP_SNAPSHOT_DIR=<dir> (renders the loading window, which test mode never
//   puts on screen, to <dir>/NNN-<stage>.png at each stage event; previews without Screen Recording permission).
//
// launcher-fixes (openspec/changes/launcher-fixes):
// - Reopen during loading (a Dock/Finder click before the game's window is up) brings the loading window forward
//   instead of "already running". "Already running" is only the second-launch path (the play loop's exit code 2).
// - Shutdown window: from a quit request (Dock Quit, Cmd-Q, SIGTERM) or the play loop's first `[shutdown] {json}`
//   line (the game exited), the loading window turns into "Saving and shutting down the world…" and stays on screen
//   (ordered front without taking focus) until the play loop has exited, i.e. everything has stopped; then the app
//   exits. Each shutdown line is recorded as a hook event {"kind":"shutdown_stage", ...} with the window's state
//   ("visible": AppKit's view, "onscreen": the window server's); "shutdown_window" events mark shown/closed.
//   A problem during shutdown still ends in the error dialog.
// - More test-only switches (HS_APP_TEST_DISABLE): loading_reopen (the old "already running" on a reopen during
//   loading), loading_reopen_hide (a reopen during loading hides the loading window), shutdown_window (no shutdown
//   window), shutdown_early_close (the shutdown window closes at the first shutdown stage).
//
// save-backups (openspec/changes/save-backups):
// - Opening Moonglade with the Option key held shows the Backups window instead of starting the game: the backups
//   from `./play --backups list --json` (newest first: date, kind, characters), Restore (an NSAlert confirmation that
//   says what is rewound and that a safety backup is taken first; then `./play --backups restore <id> --json`), Play
//   (closes the window and starts the game as usual) and Quit. Closing the window exits 0 with nothing started.
// - `[notice] ...` lines of the play loop (e.g. "Last backup failed: ...") show in the loading window's notice line;
//   the Backups window shows the same notice from the list's "last_failure". Each is recorded as a hook event.
// - The shutdown window needs nothing new: the play loop's `shutdown_backup` stage ("Backing up your save") arrives
//   as a [shutdown] line like the others.
// - Test-only, honoured only with the test hook: HS_APP_TEST_OPTION=1 (the Option key reported held at launch) and
//   HS_APP_TEST_BACKUPS=<steps> (scripted clicks in the Backups window, ';'-separated: select:<id>, restore,
//   confirm | cancel (the answer to the confirmation), wait:<file> (until the file exists), play, close).
//   HS_APP_TEST_RESTORE_TARGET=hsbk_<name> passes `--target` to the restore (tests restore into scratch schemas only).
//   The ./play --backups calls get --allow-test-client-dir only when the app itself was given it.
//
// seamless-experience (openspec/changes/seamless-experience, mac-app):
// - Friendly error dialogs: the main text says what happened in plain words (no server, database, MySQL, port, realm or
//   SOAP; the stage it stopped at is named by its friendly name, kFriendlyStage) and a "Show details" disclosure in
//   the alert's accessory view reveals the technical error (the play loop's error, the server's own words) and the
//   log path. The hook event carries "text" (the main text) and "details". In test mode with HS_APP_SNAPSHOT_DIR the
//   alert is also rendered off screen, collapsed and expanded, to <dir>/dialog-<id>[-details].png.
// - Restore feedback: after a restore the Backups window's status line says "Restored. Your save is now from <date>."
//   or "The restore didn't happen: <reason>." (hook event {"kind":"backups_status"}), before Play or Quit.
//
// release-first-launch (openspec/changes/release-first-launch, design 1-3; tasks 2.3, 5.3):
// - Release mode: a bundle without Resources/repo-path whose Resources/Moonglade/VERSION exists (the release-build
//   payload inside Moonglade.app). The payload is found from the bundle itself (so moving the app or Gatekeeper
//   translocation just works), the play loop is the payload's own Python: `python/bin/python3 -I -B -m hslib.play` (never
//   a python3 from PATH, and PATH never names /opt/homebrew or /usr/local), and app.log / play-last.json live in HOME
//   (~/Library/Application Support/Moonglade/logs and run). Dev mode (repo-path) is unchanged.
// - Release test switches (HS_RELEASE_HOME, HS_RELEASE_PORTS, HS_RELEASE_DOWNLOAD_BASE, HS_FIRSTLAUNCH_TEST): passed to
//   the play loop only with the test hook and --allow-test-release on the command line (the play loop then also needs
//   a live harness run); otherwise the app refuses, naming them, and starts and writes nothing. HS_RELEASE_HOME must
//   be below a .../harness/out/release-scratch/ folder. HS_APP_TEST_CLIENT_PICK is the app's own (the first-launch
//   folder pick, honoured only with the test hook) and never reaches the play loop.
#import <Cocoa/Cocoa.h>
#import <objc/runtime.h>
#include <signal.h>
#include <fcntl.h>
#include <unistd.h>
#include <pwd.h>
#include <limits.h>
#include <stdlib.h>

static NSString *const kHookEnv = @"HS_APP_TEST_HOOK";
static NSString *const kDisableEnv = @"HS_APP_TEST_DISABLE";
static NSString *const kSnapshotEnv = @"HS_APP_SNAPSHOT_DIR";
static NSString *const kReopenNote = @"local.moonglade.reopen";
static NSString *const kWindowNote = @"local.moonglade.window";
static NSString *const kTestOptionEnv = @"HS_APP_TEST_OPTION";
static NSString *const kTestBackupsEnv = @"HS_APP_TEST_BACKUPS";
static NSString *const kTestTargetEnv = @"HS_APP_TEST_RESTORE_TARGET";
static NSString *const kReleaseFlag = @"--allow-test-release";
static NSString *const kClientPickEnv = @"HS_APP_TEST_CLIENT_PICK";
static NSArray<NSString *> *ReleaseSwitches(void) {
    return @[@"HS_RELEASE_HOME", @"HS_RELEASE_PORTS", @"HS_RELEASE_DOWNLOAD_BASE", @"HS_FIRSTLAUNCH_TEST"];
}

// The loading window's background, drawn in drawRect: so an off-screen render (snapshot) has it too.
@interface HSBackgroundView : NSView
@end
@implementation HSBackgroundView
- (void)drawRect:(NSRect)r {
    [[NSColor windowBackgroundColor] setFill];
    NSRectFill(r);
}
@end

@interface HSLauncher : NSObject <NSApplicationDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate>
@property(strong) NSString *repo;                // dev: the checkout; release: the payload (realpath)
@property BOOL releaseMode;
@property(strong) NSString *home;                // where logs/ and run/ are (dev: the checkout; release: HOME)
@property(strong) NSString *loopExe;             // dev: /bin/bash; release: the payload's python3
@property(strong) NSArray<NSString *> *loopPrefix;   // dev: [<repo>/play]; release: [-I, -B, -m, hslib.play]
@property(strong) NSString *clientPick;          // release, test hook only: the scripted first-launch folder pick
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
// loading stages
@property(strong) NSTextField *stageLabel;
@property(strong) NSProgressIndicator *bar;
@property(strong) NSTimer *barTimer;
@property(strong) NSDictionary *stage;          // the latest stage event
@property double stageSince;                     // when that stage started (launcher clock)
@property double targetOverall;
@property double shownOverall;
@property(strong) NSString *snapshotDir;
@property int snapshotCount;
@property(strong) NSSet<NSString *> *disabled;   // test-only switches (HS_APP_TEST_DISABLE)
// the game (hand-off, focus forwarding)
@property(strong) NSRunningApplication *game;
@property BOOL handedOff;
@property double lastForward;
@property double handoffAt;
// shutdown (launcher-fixes)
@property BOOL shutdownMode;
@property(strong) NSTextField *titleLabel;
// save-backups
@property(strong) NSTextField *noticeLabel;          // the loading window's "[notice]" line
@property(strong) NSArray<NSString *> *playArgs;
@property(strong) NSDictionary *playEnv;
@property(strong) NSWindow *backupsWindow;
@property(strong) NSTableView *backupsTable;
@property(strong) NSTextField *backupsNotice;
@property(strong) NSTextField *backupsStatus;
@property(strong) NSButton *restoreButton;
@property(strong) NSButton *playButton;
@property(strong) NSButton *quitButton;
@property(strong) NSArray<NSDictionary *> *backups;
@property(strong) NSMutableArray<NSString *> *script;   // test-only scripted steps
@property(strong) NSTask *restoreTask;
@property BOOL leavingBackups;                         // Play chosen: closing the window doesn't quit
@property(strong) NSString *testRestoreTarget;         // test-only: scratch schemas a restore goes to
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

- (void)ensureWindow {
    if (self.statusWindow) return;
    NSRect r = NSMakeRect(0, 0, 520, 200);
    NSWindow *w = [[NSWindow alloc] initWithContentRect:r
                                              styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskMiniaturizable
                                                backing:NSBackingStoreBuffered defer:NO];
    w.title = @"Moonglade";
    w.releasedWhenClosed = NO;
    HSBackgroundView *root = [[HSBackgroundView alloc] initWithFrame:r];
    w.contentView = root;
    NSImageView *iv = [NSImageView imageViewWithImage:[NSApp applicationIconImage]];
    iv.frame = NSMakeRect(24, 92, 84, 84);
    [root addSubview:iv];
    NSTextField *title = [NSTextField labelWithString:@"Moonglade is starting…"];
    title.font = [NSFont boldSystemFontOfSize:16];
    title.frame = NSMakeRect(124, 146, 372, 24);
    title.lineBreakMode = NSLineBreakByTruncatingTail;
    [root addSubview:title];
    NSProgressIndicator *bar = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(124, 120, 372, 20)];
    bar.style = NSProgressIndicatorStyleBar;
    bar.indeterminate = NO;
    bar.minValue = 0;
    bar.maxValue = 1;
    bar.doubleValue = 0;
    [root addSubview:bar];
    NSTextField *detail = [NSTextField wrappingLabelWithString:@"Starting the local server. This takes a minute or two."];
    detail.font = [NSFont systemFontOfSize:11];
    detail.textColor = [NSColor secondaryLabelColor];
    detail.frame = NSMakeRect(124, 58, 372, 56);
    [root addSubview:detail];
    NSTextField *notice = [NSTextField wrappingLabelWithString:@""];
    notice.font = [NSFont systemFontOfSize:11];
    notice.textColor = [NSColor systemOrangeColor];
    notice.frame = NSMakeRect(124, 12, 372, 42);
    notice.hidden = YES;
    [root addSubview:notice];
    self.noticeLabel = notice;
    self.stageLabel = title;
    self.titleLabel = title;
    self.bar = bar;
    self.statusDetail = detail;
    self.statusWindow = w;
    [w center];
    self.barTimer = [NSTimer scheduledTimerWithTimeInterval:0.05 target:self selector:@selector(animateBar)
                                                   userInfo:nil repeats:YES];
}

- (void)showStatus:(NSString *)ident text:(NSString *)text {
    [self record:@{@"kind": @"status", @"id": ident, @"text": text}];
    [self ensureWindow];
    if (!self.stage) self.stageLabel.stringValue = text;
    // shown in test mode too (dialogs aren't): a launcher with a key window keeps activation while loading, as in
    // real use, so the hand-off checks exercise the real path
    [NSApp activateIgnoringOtherApps:YES];
    [self.statusWindow makeKeyAndOrderFront:nil];
}

// Where the bar should be now: the last event's overall, or for a stage without measurable progress a slow creep
// toward 90 % of its slot (so it never looks frozen and never passes the stage).
- (double)wantedOverall {
    double want = self.targetOverall;
    NSDictionary *st = self.stage;
    NSString *state = st[@"state"];
    NSArray *slot = st[@"slot"];
    if (st && [st[@"fraction"] isKindOfClass:[NSNull class]] && ([state isEqual:@"start"] || [state isEqual:@"progress"])
        && [slot isKindOfClass:[NSArray class]] && slot.count == 2) {
        double lo = [slot[0] doubleValue], hi = [slot[1] doubleValue];
        double expect = MAX([st[@"expect_s"] doubleValue], 0.5);
        double t = [NSDate timeIntervalSinceReferenceDate] - self.stageSince;
        want = MAX(want, lo + (hi - lo) * 0.9 * (1 - exp(-t / expect)));
    }
    return want;
}

- (void)animateBar {
    double want = [self wantedOverall];
    if (want <= self.shownOverall) return;
    self.shownOverall = MIN(want, self.shownOverall + MAX((want - self.shownOverall) * 0.2, 0.002));
    self.bar.doubleValue = self.shownOverall;
}

- (void)onStage:(NSDictionary *)ev {
    NSMutableDictionary *rec = [ev mutableCopy];
    if (rec[@"t"]) { rec[@"play_t"] = rec[@"t"]; [rec removeObjectForKey:@"t"]; }
    rec[@"kind"] = @"stage";
    [self record:rec];
    if (self.shutdownMode) return;                   // the window shows the shutdown now
    NSString *sid = [ev[@"id"] isKindOfClass:[NSString class]] ? ev[@"id"] : @"";
    if (![sid isEqual:self.stage[@"id"]] || [ev[@"state"] isEqual:@"start"])
        self.stageSince = [NSDate timeIntervalSinceReferenceDate];
    self.stage = ev;
    self.targetOverall = MAX(self.targetOverall, [ev[@"overall"] doubleValue]);
    [self ensureWindow];
    NSString *label = [ev[@"label"] isKindOfClass:[NSString class]] ? ev[@"label"] : sid;
    id frac = ev[@"fraction"];
    if ([sid isEqual:@"world"] && [frac isKindOfClass:[NSNumber class]] && [ev[@"state"] isEqual:@"progress"])
        label = [NSString stringWithFormat:@"%@  %d%%", label, (int)lround([frac doubleValue] * 100)];
    if ([ev[@"state"] isEqual:@"failed"]) label = [NSString stringWithFormat:@"Stopped: %@", label];
    if (!self.stopRequested && !self.shutdownMode) self.stageLabel.stringValue = label;
    if ([sid isEqual:@"ready"]) {
        NSDictionary *win = [ev[@"window"] isKindOfClass:[NSDictionary class]] ? ev[@"window"] : nil;
        if (win[@"pid"]) [self gameWindowUp:[win[@"pid"] intValue] via:@"ready_stage"];
    }
    if (self.snapshotDir) {                      // the bar as it will settle for this event
        self.shownOverall = MAX(self.shownOverall, [self wantedOverall]);
        self.bar.doubleValue = self.shownOverall;
        [self snapshot:[NSString stringWithFormat:@"%@-%@", sid, ev[@"state"]]];
    }
}

- (void)snapshot:(NSString *)tag {
    NSView *v = self.statusWindow.contentView;
    [v layoutSubtreeIfNeeded];
    NSBitmapImageRep *rep = [v bitmapImageRepForCachingDisplayInRect:v.bounds];
    [v cacheDisplayInRect:v.bounds toBitmapImageRep:rep];
    NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
    NSString *f = [self.snapshotDir stringByAppendingPathComponent:
                   [NSString stringWithFormat:@"%03d-%@.png", ++self.snapshotCount, tag]];
    [png writeToFile:f atomically:YES];
}

// The play loop's lines are written for a terminal; a few read wrong in the app's window.
- (NSString *)appWording:(NSString *)msg {
    return [msg stringByReplacingOccurrencesOfString:@"Press Ctrl-C here to stop everything."
                                          withString:@"To stop everything, quit Moonglade from the Dock."];
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

// The loading stages as the error dialog's main text names them (stages.py ids). The loading window's labels name the
// login server and the database; the main text of an error doesn't (mac-app "Friendly launcher errors").
static NSDictionary<NSString *, NSString *> *kFriendlyStage(void) {
    return @{@"prepare": @"Getting ready", @"database": @"Opening your save", @"login": @"Getting ready to play",
             @"world": @"Loading the world", @"bots": @"Waking the AI players", @"account": @"Checking your profile",
             @"game_setup": @"Preparing the game", @"launching": @"Launching the game", @"ready": @"Ready"};
}

// A friendly error: plain main text, and a "Show details" disclosure with the technical text (error + log path).
- (NSAlert *)errorAlert:(NSString *)title text:(NSString *)text details:(NSString *)details {
    NSAlert *a = [NSAlert new];
    a.alertStyle = NSAlertStyleCritical;
    a.messageText = title;
    a.informativeText = text;
    [a addButtonWithTitle:@"OK"];
    if (self.logPath) [a addButtonWithTitle:@"Show Log"];
    NSView *acc = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 420, 24)];
    NSButton *tri = [[NSButton alloc] initWithFrame:NSMakeRect(0, 2, 20, 20)];
    tri.bezelStyle = NSBezelStyleDisclosure;
    tri.buttonType = NSButtonTypePushOnPushOff;
    tri.title = @"";
    NSButton *label = [NSButton buttonWithTitle:@"Show details" target:nil action:nil];
    label.bordered = NO;
    label.frame = NSMakeRect(22, 2, 200, 20);
    label.alignment = NSTextAlignmentLeft;
    NSScrollView *sv = [[NSScrollView alloc] initWithFrame:NSMakeRect(0, 0, 420, 160)];
    sv.hasVerticalScroller = YES;
    sv.borderType = NSBezelBorder;
    NSTextView *tv = [[NSTextView alloc] initWithFrame:sv.bounds];
    tv.editable = NO;
    tv.selectable = YES;
    tv.font = [NSFont userFixedPitchFontOfSize:11];
    tv.string = details;
    tv.verticallyResizable = YES;
    tv.textContainer.widthTracksTextView = YES;
    sv.documentView = tv;
    sv.hidden = YES;
    [acc addSubview:tri];
    [acc addSubview:label];
    [acc addSubview:sv];
    a.accessoryView = acc;
    __weak NSAlert *wa = a;
    void (^toggle)(BOOL) = ^(BOOL open) {
        tri.state = open ? NSControlStateValueOn : NSControlStateValueOff;
        label.title = open ? @"Hide details" : @"Show details";
        sv.hidden = !open;
        acc.frame = NSMakeRect(0, 0, 420, open ? 190 : 24);
        tri.frame = NSMakeRect(0, acc.frame.size.height - 22, 20, 20);
        label.frame = NSMakeRect(22, acc.frame.size.height - 22, 200, 20);
        [wa layout];
    };
    objc_setAssociatedObject(tri, "hs_toggle", toggle, OBJC_ASSOCIATION_COPY_NONATOMIC);
    objc_setAssociatedObject(label, "hs_toggle", toggle, OBJC_ASSOCIATION_COPY_NONATOMIC);
    objc_setAssociatedObject(label, "hs_tri", tri, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    tri.target = self;
    tri.action = @selector(detailsToggled:);
    label.target = self;
    label.action = @selector(detailsToggled:);
    toggle(NO);
    return a;
}

- (void)detailsToggled:(NSButton *)sender {
    void (^toggle)(BOOL) = objc_getAssociatedObject(sender, "hs_toggle");
    NSButton *tri = objc_getAssociatedObject(sender, "hs_tri") ?: sender;
    BOOL open = (sender == tri) ? tri.state == NSControlStateValueOn : tri.state != NSControlStateValueOn;
    if (toggle) toggle(open);
}

// Test mode: render the alert off screen (never shown), collapsed and with its details open.
- (void)snapshotAlert:(NSAlert *)a tag:(NSString *)tag {
    if (!self.snapshotDir) return;
    for (int open = 0; open < 2; open++) {
        NSView *acc = a.accessoryView;
        for (NSView *v in acc.subviews)
            if ([v isKindOfClass:[NSButton class]] && objc_getAssociatedObject(v, "hs_toggle") && !objc_getAssociatedObject(v, "hs_tri")) {
                void (^toggle)(BOOL) = objc_getAssociatedObject(v, "hs_toggle");
                toggle(open == 1);
            }
        [a layout];
        NSView *v = a.window.contentView;
        [v layoutSubtreeIfNeeded];
        NSBitmapImageRep *rep = [v bitmapImageRepForCachingDisplayInRect:v.bounds];
        [v cacheDisplayInRect:v.bounds toBitmapImageRep:rep];
        NSData *png = [rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        [png writeToFile:[self.snapshotDir stringByAppendingPathComponent:
                          [NSString stringWithFormat:@"dialog-%@%@.png", tag, open ? @"-details" : @""]] atomically:YES];
    }
}

- (void)errorDialog:(NSString *)ident title:(NSString *)title text:(NSString *)text details:(NSString *)details {
    [self record:@{@"kind": @"dialog", @"id": ident, @"title": title, @"text": text, @"details": details,
                   @"details_control": @"Show details"}];
    if ([self testMode]) {
        if (self.snapshotDir) [self snapshotAlert:[self errorAlert:title text:text details:details] tag:ident];
        return;
    }
    [self hideStatus];
    NSAlert *a = [self errorAlert:title text:text details:details];
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
    NSDictionary *penv = NSProcessInfo.processInfo.environment;
    self.hookPath = penv[kHookEnv];
    self.disabled = [NSSet set];
    self.playLines = [NSMutableArray array];
    self.outLines = [NSMutableArray array];
    self.partial = [NSMutableString string];
    [self installSignalHandlers];

    NSString *repo = [self resource:@"repo-path"];
    NSString *play = [repo stringByAppendingPathComponent:@"play"];
    NSString *payload = [[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:@"Moonglade"];
    NSFileManager *fm = [NSFileManager defaultManager];
    self.releaseMode = repo.length == 0
        && [fm fileExistsAtPath:[payload stringByAppendingPathComponent:@"VERSION"]];
    NSArray *argv = NSProcessInfo.processInfo.arguments;
    BOOL releaseFlag = [argv containsObject:kReleaseFlag];
    // the release test switches (and the app's own folder pick): only a release app with the test hook and the test
    // flag may pass them on; anything else (the dev app included) refuses before starting or writing anything
    NSMutableArray *set = [NSMutableArray array];
    for (NSString *k in [ReleaseSwitches() arrayByAddingObject:kClientPickEnv]) if ([penv[k] length]) [set addObject:k];
    NSString *scratch = penv[@"HS_RELEASE_HOME"];
    BOOL scratchOK = !scratch.length || ([scratch isAbsolutePath]
                                          && [scratch rangeOfString:@"/harness/out/release-scratch/"].location != NSNotFound);
    if (set.count && !(self.releaseMode && [self testMode] && releaseFlag && scratchOK)) {
        NSString *why = [NSString stringWithFormat:@"%@ %@ test-only (the harness's release checks) and %@ refused "
                         @"%@. Unset %@ and open Moonglade again.",
                         [set componentsJoinedByString:@", "], set.count == 1 ? @"is" : @"are",
                         set.count == 1 ? @"is" : @"are",
                         !self.releaseMode ? @"by the dev app"
                         : !scratchOK ? @"unless HS_RELEASE_HOME is below .../harness/out/release-scratch/"
                         : [NSString stringWithFormat:@"without the test hook and %@", kReleaseFlag],
                         set.count == 1 ? @"it" : @"them"];
        if ([self testMode]) {
            int fd = open(self.hookPath.fileSystemRepresentation, O_WRONLY | O_CREAT | O_APPEND, 0644);
            NSDictionary *evs[2] = {@{@"kind": @"refused", @"switches": set, @"text": why, @"pid": @(getpid()),
                                      @"bundle": [[NSBundle mainBundle] bundlePath], @"t": [self now]},
                                    @{@"kind": @"exit", @"code": @2, @"pid": @(getpid()), @"t": [self now]}};
            for (int i = 0; i < 2 && fd >= 0; i++) {
                NSMutableData *d = [[NSJSONSerialization dataWithJSONObject:evs[i] options:NSJSONWritingSortedKeys
                                                                     error:nil] mutableCopy];
                [d appendBytes:"\n" length:1];
                write(fd, d.bytes, d.length);
            }
            if (fd >= 0) close(fd);
        } else {
            NSAlert *a = [NSAlert new];
            a.messageText = @"Moonglade can't start";
            a.informativeText = why;
            [a runModal];
        }
        exit(2);                                        // nothing started, nothing written (no app.log either)
    }
    if (self.releaseMode) {
        char real[PATH_MAX];
        repo = realpath(payload.fileSystemRepresentation, real) ? [fm stringWithFileSystemRepresentation:real
                                                                   length:strlen(real)] : payload;
        play = nil;
        struct passwd *pw = getpwuid(getuid());
        NSString *userHome = pw ? [fm stringWithFileSystemRepresentation:pw->pw_dir length:strlen(pw->pw_dir)]
                                : NSHomeDirectory();
        self.home = scratch.length ? scratch
                  : [userHome stringByAppendingPathComponent:@"Library/Application Support/Moonglade"];
        self.loopExe = [repo stringByAppendingPathComponent:@"python/bin/python3"];
        self.loopPrefix = @[@"-I", @"-B", @"-m", @"hslib.play"];   // -B: never write __pycache__ into the app
        if (![fm isExecutableFileAtPath:self.loopExe]) {
            [self dialog:@"payload_missing" title:@"Moonglade is incomplete"
                    text:@"This copy of Moonglade is missing parts it needs. Download it again and replace this "
                         @"copy.\n\nNo log was written."
                   style:NSAlertStyleCritical showLog:NO];
            [self finish:1];
        }
    } else if (repo.length == 0 || ![fm isExecutableFileAtPath:play]) {
        [self dialog:@"repo_missing" title:@"Moonglade can't find its folder"
                text:[NSString stringWithFormat:@"This app runs the game from the Moonglade folder at\n%@\nbut that "
                      @"folder (or its `play` script) isn't there any more. If you moved the folder, rebuild the app "
                      @"from it with `harness/hs app`.\n\nNo log was written.", repo.length ? repo : @"(not recorded)"]
               style:NSAlertStyleCritical showLog:NO];
        [self finish:1];
    } else {
        self.home = repo;
        self.loopExe = @"/bin/bash";
        self.loopPrefix = @[play];
    }
    self.repo = repo;
    NSString *logs = [self.home stringByAppendingPathComponent:self.releaseMode ? @"logs" : @"env/logs"];
    [[NSFileManager defaultManager] createDirectoryAtPath:logs withIntermediateDirectories:YES attributes:nil error:nil];
    self.logPath = [logs stringByAppendingPathComponent:@"app.log"];
    int fd = open(self.logPath.fileSystemRepresentation, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) self.logHandle = [[NSFileHandle alloc] initWithFileDescriptor:fd closeOnDealloc:YES];
    [self appLog:[NSString stringWithFormat:@"==== Moonglade.app pid %d from %@ (%@ %@)", getpid(),
                  [[NSBundle mainBundle] bundlePath], self.releaseMode ? @"release, payload" : @"repo", repo]];
    if ([self testMode]) {                             // test-only switches: never honoured without the test hook
        if ([penv[kDisableEnv] length])
            self.disabled = [NSSet setWithArray:[penv[kDisableEnv] componentsSeparatedByString:@","]];
        if ([penv[kSnapshotEnv] length]) self.snapshotDir = penv[kSnapshotEnv];
        if (self.disabled.count || self.snapshotDir)
            [self record:@{@"kind": @"test_switches", @"disabled": self.disabled.allObjects,
                           @"snapshot_dir": self.snapshotDir ?: [NSNull null]}];
        if (self.releaseMode && [penv[kClientPickEnv] length]) self.clientPick = penv[kClientPickEnv];
    } else if ([penv[kDisableEnv] length] || [penv[kSnapshotEnv] length]) {
        [self appLog:@"ignoring HS_APP_TEST_DISABLE / HS_APP_SNAPSHOT_DIR: test-only, need the test hook"];
    }
    if ([penv[kClientPickEnv] length] && !self.clientPick)
        [self appLog:@"ignoring HS_APP_TEST_CLIENT_PICK: test-only, needs the test hook and a release app"];
    [[NSDistributedNotificationCenter defaultCenter] addObserver:self selector:@selector(gameWindowNote:)
                                                            name:kWindowNote object:repo
                                              suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];

    NSMutableArray *args = [NSMutableArray arrayWithArray:self.loopPrefix];
    for (NSUInteger i = 1; i < argv.count; i++)
        if (![argv[i] hasPrefix:@"-psn_"]) [args addObject:argv[i]];

    NSMutableDictionary *env = [NSProcessInfo.processInfo.environment mutableCopy];
    if (self.releaseMode) {
        // the payload's Python only (5.3): no Homebrew or /usr/local on PATH, nothing from PYTHON* (-I ignores them)
        env[@"PATH"] = @"/usr/bin:/bin:/usr/sbin:/sbin";
        if (!([self testMode] && releaseFlag))
            for (NSString *k in ReleaseSwitches()) [env removeObjectForKey:k];
    } else {
        NSString *pyDir = [self resource:@"python-dir"];
        NSString *path = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
        env[@"PATH"] = pyDir.length ? [NSString stringWithFormat:@"%@:%@", pyDir, path] : path;
    }
    [env removeObjectForKey:kClientPickEnv];           // the app's own switch, never the play loop's
    [env removeObjectForKey:kHookEnv];                 // the hook is the app's, not the play loop's
    [env removeObjectForKey:kDisableEnv];
    [env removeObjectForKey:kSnapshotEnv];
    [env removeObjectForKey:kTestOptionEnv];
    [env removeObjectForKey:kTestBackupsEnv];
    [env removeObjectForKey:kTestTargetEnv];
    env[@"HS_APP_BUNDLE"] = [[NSBundle mainBundle] bundlePath];   // the shim shows this bundle's icon
    env[@"PYTHONUNBUFFERED"] = @"1";
    env[@"HS_FROM_APP"] = @"1";                        // ./play then keeps passwords out of app.log (play.shown_password)
    self.playArgs = args;
    self.playEnv = env;

    // save-backups: Option held at launch opens the Backups window instead of the game
    BOOL option = ([NSEvent modifierFlags] & NSEventModifierFlagOption) != 0;
    if ([self testMode]) {
        if ([penv[kTestOptionEnv] isEqual:@"1"]) option = YES;
        if ([penv[kTestBackupsEnv] length])
            self.script = [[penv[kTestBackupsEnv] componentsSeparatedByString:@";"] mutableCopy];
        if ([penv[kTestTargetEnv] length]) self.testRestoreTarget = penv[kTestTargetEnv];
    } else if ([penv[kTestOptionEnv] length] || [penv[kTestBackupsEnv] length] || [penv[kTestTargetEnv] length]) {
        [self appLog:@"ignoring HS_APP_TEST_OPTION / HS_APP_TEST_BACKUPS / HS_APP_TEST_RESTORE_TARGET: test-only, "
                     @"need the test hook"];
    }
    if (option) {
        [self record:@{@"kind": @"option_launch", @"injected": @([self testMode] && [penv[kTestOptionEnv] isEqual:@"1"])}];
        [self showBackups];
        return;
    }
    [self startPlay];
}

- (void)startPlay {
    NSArray *args = self.playArgs;
    NSDictionary *env = self.playEnv;
    NSString *repo = self.repo;
    unsigned long long size = [[[NSFileManager defaultManager] attributesOfItemAtPath:self.logPath error:nil] fileSize];
    self.tailOffset = size;
    NSTask *t = [NSTask new];
    t.executableURL = [NSURL fileURLWithPath:self.loopExe];
    t.arguments = args;
    t.currentDirectoryURL = [NSURL fileURLWithPath:self.releaseMode ? self.home : repo];
    t.environment = env;
    t.standardInput = [NSFileHandle fileHandleWithNullDevice];
    t.standardOutput = self.logHandle ?: [NSFileHandle fileHandleWithNullDevice];
    t.standardError = self.logHandle ?: [NSFileHandle fileHandleWithNullDevice];
    __weak HSLauncher *weakSelf = self;
    t.terminationHandler = ^(NSTask *task) {
        dispatch_async(dispatch_get_main_queue(), ^{ [weakSelf playExited:task]; });
    };
    [self showStatus:@"starting" text:@"Moonglade is starting…"];
    NSError *err = nil;
    if (![t launchAndReturnError:&err]) {
        [self errorDialog:@"error" title:@"Moonglade couldn't start"
                     text:@"Moonglade couldn't start the game. Try opening it again; if it keeps happening, Show "
                          @"details says what went wrong."
                  details:[NSString stringWithFormat:@"The play loop couldn't be started: %@\n\nThe full log is at:\n%@",
                           err.localizedDescription, self.logPath]];
        [self finish:1];
    }
    self.task = t;
    [self appLog:[NSString stringWithFormat:@"play loop pid %d: %@%@", t.processIdentifier,
                  self.releaseMode ? [self.loopExe stringByAppendingString:@" "] : @"",
                  [args componentsJoinedByString:@" "]]];
    self.tailTimer = [NSTimer scheduledTimerWithTimeInterval:0.25 target:self selector:@selector(tail)
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
        if ([l hasPrefix:@"[stage] "]) {
            NSData *jd = [[l substringFromIndex:8] dataUsingEncoding:NSUTF8StringEncoding];
            id ev = jd ? [NSJSONSerialization JSONObjectWithData:jd options:0 error:nil] : nil;
            if ([ev isKindOfClass:[NSDictionary class]]) [self onStage:ev];
            continue;
        }
        if ([l hasPrefix:@"[notice] "]) {                 // save-backups: e.g. "Last backup failed: ..."
            NSString *msg = [l substringFromIndex:9];
            [self record:@{@"kind": @"notice", @"text": msg, @"where": @"loading"}];
            [self ensureWindow];
            self.noticeLabel.stringValue = msg;
            self.noticeLabel.hidden = NO;
            continue;
        }
        if ([l hasPrefix:@"[shutdown] "]) {
            NSData *jd = [[l substringFromIndex:11] dataUsingEncoding:NSUTF8StringEncoding];
            id ev = jd ? [NSJSONSerialization JSONObjectWithData:jd options:0 error:nil] : nil;
            if ([ev isKindOfClass:[NSDictionary class]]) [self onShutdown:ev];
            continue;
        }
        if (![l hasPrefix:@"[play] "]) {
            NSString *t = [l stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceCharacterSet]];
            if (t.length) [self.outLines addObject:t.length > 300 ? [t substringToIndex:300] : t];
            if (self.outLines.count > 50) [self.outLines removeObjectAtIndex:0];
            continue;
        }
        NSString *msg = [l substringFromIndex:7];
        [self.playLines addObject:msg];
        if (self.playLines.count > 50) [self.playLines removeObjectAtIndex:0];
        if (!self.handedOff && !self.stopRequested && !self.shutdownMode) {
            self.statusDetail.stringValue = [self appWording:msg];
            [self record:@{@"kind": @"detail", @"text": self.statusDetail.stringValue}];
        }
        if (!self.clientStarted && [msg hasPrefix:@"The game is starting"]) {
            self.clientStarted = YES;                 // the window stays until the game's window is up (hand-off)
            [self record:@{@"kind": @"status", @"id": @"client_launched", @"text": msg}];
            if ([self.disabled containsObject:@"early_hide"])   // test-only counterexample: hide here, as before
                [self hideLoading:@"at client launch (test switch early_hide)"];
        }
    }
}

// ------------------------------------------------------------------ the play loop ended

- (NSDictionary *)playRecord:(int)pid {
    NSString *p = [self.home stringByAppendingPathComponent:self.releaseMode ? @"run/play-last.json"
                                                                              : @"env/run/play-last.json"];
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
            [parts addObject:@"macOS blocked access to the Moonglade folder. Allow it in System Settings > Privacy & "
                             @"Security > Files and Folders > Moonglade (Documents Folder), then open the app again."];
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
    if (self.shutdownMode)                             // everything has stopped (the play loop is gone): close it
        [self record:@{@"kind": @"shutdown_window", @"state": @"closed", @"code": @(code),
                       @"visible_before": @(self.statusWindow.visible), @"onscreen_before": @([self windowOnScreen])}];
    [self hideStatus];
    if (code == 0) [self finish:0];
    if (code == 2) {                                    // play.py EXIT_BUSY: the single-instance lock is held
        [self dialog:@"already_running" title:@"Moonglade is already running"
                text:@"The game is already open (or still starting). Use that window, or quit the game first."
               style:NSAlertStyleInformational showLog:NO];
        [self finish:0];
    }
    NSDictionary *rec = [self playRecord:task.processIdentifier];
    if (self.stopRequested && code >= 128 && ![[rec[@"cleanup"] objectForKey:@"remaining"] count]) [self finish:0];
    BOOL started = [rec[@"client_started"] boolValue] || self.clientStarted;
    NSString *title = started ? @"Moonglade stopped because of a problem" : @"Moonglade couldn't start";
    NSString *stageId = nil, *stageLabel = nil;        // the loading stage it stopped at (play-last.json, else ours)
    if ([rec[@"stage"] isKindOfClass:[NSDictionary class]] && [rec[@"stage"][@"label"] isKindOfClass:[NSString class]]) {
        stageLabel = rec[@"stage"][@"label"];
        stageId = [rec[@"stage"][@"id"] isKindOfClass:[NSString class]] ? rec[@"stage"][@"id"] : nil;
    } else if (!started && [self.stage[@"label"] isKindOfClass:[NSString class]] && ![self.stage[@"state"] isEqual:@"done"]) {
        stageLabel = self.stage[@"label"];
        stageId = [self.stage[@"id"] isKindOfClass:[NSString class]] ? self.stage[@"id"] : nil;
    }
    NSString *friendlyStage = (stageId ? kFriendlyStage()[stageId] : nil) ?: (stageLabel ? @"Getting ready" : nil);
    NSString *at = friendlyStage ? [NSString stringWithFormat:@" It stopped at: %@.", friendlyStage] : @"";
    NSString *text = started
        ? @"The game stopped unexpectedly. Open Moonglade again to keep playing."
        : [NSString stringWithFormat:@"Moonglade couldn't get the game ready.%@ Try opening it again; if it keeps "
                                     @"happening, Show details says what went wrong.", at];
    NSString *stageLine = stageLabel ? [NSString stringWithFormat:@"Stage: %@\n\n", stageLabel] : @"";
    NSString *details = [NSString stringWithFormat:@"%@%@\n\nThe full log is at:\n%@", stageLine,
                         [self problemFrom:rec code:code], self.logPath];
    [self errorDialog:@"error" title:title text:text details:details];
    [self finish:(code > 0 && code < 256) ? code : 1];
}

// ------------------------------------------------------------------ reopen, quit, signals

// ------------------------------------------------------------------ the game's window: hand-off and focus forwarding

- (void)gameWindowNote:(NSNotification *)n {
    int pid = [n.userInfo[@"pid"] intValue];
    [self record:@{@"kind": @"window_note", @"game_pid": @(pid), @"handed_off_before": @(self.handedOff)}];
    if (pid > 0) [self gameWindowUp:pid via:@"shim"];
}

- (void)gameWindowUp:(int)pid via:(NSString *)via {
    if (self.handedOff || !self.task.running || self.stopRequested) return;
    NSRunningApplication *g = [NSRunningApplication runningApplicationWithProcessIdentifier:pid];
    if (!g) {
        [self record:@{@"kind": @"handoff", @"game_pid": @(pid), @"via": via, @"ok": @NO, @"why": @"no such app"}];
        return;
    }
    self.game = g;
    self.handedOff = YES;
    [self record:@{@"kind": @"handoff", @"game_pid": @(pid), @"via": via, @"launcher_active": @(NSApp.isActive),
                   @"game_policy": @(g.activationPolicy)}];
    [self forwardFocus:@"handoff" attempt:0];        // first, while the loading window still holds activation
    // the loading window stays until the game's window is on screen and the game is frontmost (or 5 s), so there's
    // never a moment with nothing of Moonglade's on screen
    self.handoffAt = [NSDate timeIntervalSinceReferenceDate];
    [self pollHide];
}

- (void)pollHide {
    if (!self.statusWindow.visible || ![self gameAlive] || self.shutdownMode) return;
    BOOL win = [self gameWindowOnScreen];
    BOOL front = [[NSWorkspace sharedWorkspace] frontmostApplication].processIdentifier == self.game.processIdentifier;
    double waited = [NSDate timeIntervalSinceReferenceDate] - self.handoffAt;
    if ((win && front) || waited > 5.0) {
        [self hideLoading:(win && front) ? @"game window on screen and frontmost"
                                         : [NSString stringWithFormat:@"timeout (window %d, frontmost %d)", win, front]];
        return;
    }
    [self performSelector:@selector(pollHide) withObject:nil afterDelay:0.05];
}

- (void)hideLoading:(NSString *)why {
    if (self.shutdownMode) return;                   // the window is the shutdown window now: it stays
    [self hideStatus];
    [self record:@{@"kind": @"status", @"id": @"hidden", @"text": @"loading window hidden: the game window is up",
                   @"why": why}];
}

// The game owns an on-screen, normal-level window of real size (window server list: owner pid and bounds only, no
// Screen Recording permission involved).
- (BOOL)gameWindowOnScreen {
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
                                                 kCGNullWindowID);
    if (!list) return NO;
    BOOL found = NO;
    for (NSDictionary *w in (__bridge NSArray *)list) {
        if ([w[(id)kCGWindowOwnerPID] intValue] != self.game.processIdentifier) continue;
        if ([w[(id)kCGWindowLayer] intValue] != 0) continue;
        CGRect b;
        if (!CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)w[(id)kCGWindowBounds], &b)) continue;
        if (b.size.width >= 300 && b.size.height >= 200) { found = YES; break; }
    }
    CFRelease(list);
    return found;
}

- (BOOL)gameAlive { return self.game && !self.game.terminated; }

// Hand activation to the game. macOS 14+ only lets the active app give activation away, hence the yield; retried
// briefly while we still hold activation and the game isn't frontmost yet.
- (void)forwardFocus:(NSString *)why attempt:(int)attempt {
    if (![self gameAlive]) return;
    NSString *gate = [why isEqual:@"handoff"] ? @"handoff" : @"forward";
    if ([self.disabled containsObject:gate]) {
        if (attempt == 0) [self record:@{@"kind": @"activate", @"why": why, @"skipped": @"disabled by test switch"}];
        return;
    }
    if (attempt == 0) self.lastForward = [NSDate timeIntervalSinceReferenceDate];
    BOOL ok;
    if (@available(macOS 14.0, *)) {
        [NSApp yieldActivationToApplication:self.game];
        ok = [self.game activateFromApplication:[NSRunningApplication currentApplication]
                                        options:NSApplicationActivateAllWindows];
    } else {
        ok = [self.game activateWithOptions:NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps];
    }
    pid_t front = [[NSWorkspace sharedWorkspace] frontmostApplication].processIdentifier;
    [self record:@{@"kind": @"activate", @"why": why, @"attempt": @(attempt), @"ok": @(ok),
                   @"launcher_active": @(NSApp.isActive), @"frontmost_pid": @(front)}];
    if (attempt < 4) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.4 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            if ([self gameAlive] && [[NSWorkspace sharedWorkspace] frontmostApplication].processIdentifier
                    != self.game.processIdentifier && NSApp.isActive)
                [self forwardFocus:why attempt:attempt + 1];
        });
    }
}

// Dock/Finder click or Cmd-Tab while the game runs: unminimise it (the shim) and give it activation.
- (void)bringGameForward:(NSString *)why {
    double now = [NSDate timeIntervalSinceReferenceDate];
    if (now - self.lastForward < 0.5) return;        // reopen and didBecomeActive arrive together
    self.lastForward = now;
    if ([self.disabled containsObject:@"reopen_note"])
        [self record:@{@"kind": @"reopen_note", @"why": why, @"skipped": @"disabled by test switch"}];
    else {
        [[NSDistributedNotificationCenter defaultCenter] postNotificationName:kReopenNote object:self.repo userInfo:nil
                                                                      options:NSDistributedNotificationDeliverImmediately];
        [self record:@{@"kind": @"reopen_note", @"why": why}];
    }
    // at once, while we still hold activation: a launcher with no window on screen doesn't keep it for long (the
    // window server hands focus to the app with the topmost window), and only the active app can yield it
    [self forwardFocus:why attempt:0];
}

- (void)applicationDidBecomeActive:(NSNotification *)n {
    if (self.handedOff && [self gameAlive] && !self.stopRequested && !self.statusWindow.visible) {
        [self record:@{@"kind": @"became_active", @"forwarded": @YES}];
        [self bringGameForward:@"activate"];
    }
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)flag {
    if (self.backupsWindow && !self.task) {             // the Backups window is what Moonglade shows: bring it forward
        [NSApp activateIgnoringOtherApps:YES];
        [self.backupsWindow makeKeyAndOrderFront:nil];
        return NO;
    }
    if (self.task.running && self.handedOff && [self gameAlive] && !self.stopRequested) {
        [self record:@{@"kind": @"reopen", @"forwarded": @YES, @"game_pid": @(self.game.processIdentifier)}];
        self.lastForward = 0;                           // a click always forwards
        [self bringGameForward:@"reopen"];
        return NO;
    }
    if (self.task.running && ![self.disabled containsObject:@"loading_reopen"]) {
        // loading (no game window yet) or shutting down: bring forward what Moonglade is showing, no dialog. (The
        // only other case, the game's window gone but its shutdown not started yet, lasts a moment: just activate.)
        BOOL showing = self.shutdownMode || !self.handedOff;
        BOOL hide = showing && !self.shutdownMode && [self.disabled containsObject:@"loading_reopen_hide"];
        [self record:@{@"kind": @"reopen", @"forwarded": @NO,
                       @"showing": self.shutdownMode ? @"shutdown" : (!self.handedOff ? @"loading" : @"nothing"),
                       @"test_hide": @(hide)}];
        if (hide) {                                     // test-only counterexample
            [self.statusWindow orderOut:nil];
            return NO;
        }
        [NSApp activateIgnoringOtherApps:YES];
        if (showing) {
            [self ensureWindow];
            [self.statusWindow makeKeyAndOrderFront:nil];
        }
        return NO;
    }
    if (self.task.running) {                            // test switch loading_reopen: the old behaviour
        [self dialog:@"already_running" title:@"Moonglade is already running"
                text:(self.clientStarted ? @"The game is already open. Quit the game to stop everything."
                                         : @"Moonglade is still starting. The game will open by itself.")
               style:NSAlertStyleInformational showLog:NO];
        if (!self.clientStarted && ![self testMode]) [self.statusWindow makeKeyAndOrderFront:nil];
    }
    return NO;
}

// ------------------------------------------------------------------ the shutdown window (launcher-fixes)

// Our status window as the window server sees it: on screen right now (owner and bounds only; no permission).
- (BOOL)windowOnScreen {
    if (!self.statusWindow || self.statusWindow.windowNumber <= 0) return NO;
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow,
                                                 (CGWindowID)self.statusWindow.windowNumber);
    if (!list) return NO;
    BOOL on = NO;
    for (NSDictionary *w in (__bridge NSArray *)list)
        if ([w[(id)kCGWindowNumber] intValue] == self.statusWindow.windowNumber) on = [w[(id)kCGWindowIsOnscreen] boolValue];
    CFRelease(list);
    return on;
}

- (void)enterShutdown:(NSString *)why {
    if (self.shutdownMode) return;
    if ([self.disabled containsObject:@"shutdown_window"]) {    // test-only counterexample: no shutdown window
        [self record:@{@"kind": @"shutdown_window", @"state": @"skipped", @"why": why,
                       @"skipped": @"disabled by test switch"}];
        return;
    }
    self.shutdownMode = YES;
    [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(pollHide) object:nil];
    [self ensureWindow];
    self.titleLabel.stringValue = @"Saving and shutting down the world…";
    self.statusDetail.stringValue = @"Please don't force-quit Moonglade: it closes by itself when everything has "
                                    @"stopped. This takes up to a minute.";
    self.stage = nil;                                  // the bar now shows the shutdown's own steps
    self.targetOverall = self.shownOverall = 0;
    self.bar.doubleValue = 0;
    // on top of other windows without taking focus from whatever the player is doing now
    [self.statusWindow orderFrontRegardless];
    [self recordShown:why attempt:0];
}

// "shown" is recorded once the window server has it on screen (it lags orderFront by a display cycle), or after 1 s
- (void)recordShown:(NSString *)why attempt:(int)attempt {
    BOOL on = [self windowOnScreen];
    if (!on && attempt < 20 && self.statusWindow.visible) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.05 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [self recordShown:why attempt:attempt + 1];
        });
        return;
    }
    [self record:@{@"kind": @"shutdown_window", @"state": @"shown", @"why": why, @"waited_s": @(attempt * 0.05),
                   @"visible": @(self.statusWindow.visible), @"onscreen": @(on)}];
}

- (void)onShutdown:(NSDictionary *)ev {
    [self enterShutdown:@"play loop shutdown"];
    NSString *sid = [ev[@"id"] isKindOfClass:[NSString class]] ? ev[@"id"] : @"";
    NSString *label = [ev[@"label"] isKindOfClass:[NSString class]] ? ev[@"label"] : sid;
    if (self.shutdownMode && [self.disabled containsObject:@"shutdown_early_close"]) {   // test-only counterexample
        [self.statusWindow orderOut:nil];
    }
    if (self.shutdownMode) {
        double of = MAX([ev[@"of"] doubleValue], 1);
        double step = [ev[@"step"] doubleValue] + ([ev[@"state"] isEqual:@"start"] ? 0.5 : 1.0);
        self.targetOverall = MAX(self.targetOverall, MIN(step / of, 1.0));
        BOOL problem = [ev[@"ok"] isKindOfClass:[NSNumber class]] && ![ev[@"ok"] boolValue];
        self.statusDetail.stringValue = problem
            ? @"Something didn't stop cleanly. Moonglade will show what happened."
            : [NSString stringWithFormat:@"%@… Please don't force-quit Moonglade: it closes by itself when "
                                         @"everything has stopped.", label];
    }
    NSMutableDictionary *rec = [ev mutableCopy];
    if (rec[@"t"]) { rec[@"play_t"] = rec[@"t"]; [rec removeObjectForKey:@"t"]; }
    rec[@"kind"] = @"shutdown_stage";
    rec[@"visible"] = @(self.statusWindow.visible);
    rec[@"onscreen"] = @([self windowOnScreen]);
    rec[@"shutdown_mode"] = @(self.shutdownMode);
    [self record:rec];
}

- (void)requestStop:(NSString *)why {
    if (!self.task.running) return;
    if (!self.stopRequested) {
        self.stopRequested = YES;
        [self appLog:[NSString stringWithFormat:@"stop requested (%@): sending SIGTERM to the play loop", why]];
        [self record:@{@"kind": @"status", @"id": @"stopping", @"text": @"Moonglade is shutting down…"}];
        [self enterShutdown:why];
        [self.task terminate];
    }
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    if (self.task.running) {                            // stop the play loop first; playExited: exits the app
        [self requestStop:@"quit"];
        return NSTerminateCancel;
    }
    if (self.restoreTask.running) {                     // never cut a restore short; it ends by itself
        self.backupsStatus.stringValue = @"Restoring… Moonglade can quit when the restore has finished.";
        return NSTerminateCancel;
    }
    if (self.backupsWindow && !self.task) [self finish:0];
    return NSTerminateNow;
}

// ------------------------------------------------------------------ the Backups window (save-backups)

- (NSArray *)backupsArgs:(NSArray *)extra {
    NSMutableArray *a = [NSMutableArray arrayWithArray:self.loopPrefix];
    [a addObject:@"--backups"];
    [a addObjectsFromArray:extra];
    [a addObject:@"--json"];
    if ([self.playArgs containsObject:@"--allow-test-client-dir"]) [a addObject:@"--allow-test-client-dir"];
    if ([self.playArgs containsObject:kReleaseFlag]) [a addObject:kReleaseFlag];
    return a;
}

- (NSTask *)backupsTask:(NSArray *)extra pipe:(NSPipe *)pipe {
    NSTask *t = [NSTask new];
    t.executableURL = [NSURL fileURLWithPath:self.loopExe];
    t.arguments = [self backupsArgs:extra];
    t.currentDirectoryURL = [NSURL fileURLWithPath:self.releaseMode ? self.home : self.repo];
    t.environment = self.playEnv;
    t.standardInput = [NSFileHandle fileHandleWithNullDevice];
    t.standardOutput = pipe;
    t.standardError = self.logHandle ?: [NSFileHandle fileHandleWithNullDevice];
    return t;
}

- (NSDictionary *)parseJSON:(NSData *)d {
    id obj = d.length ? [NSJSONSerialization JSONObjectWithData:d options:0 error:nil] : nil;
    return [obj isKindOfClass:[NSDictionary class]] ? obj : nil;
}

- (void)loadBackups {
    NSPipe *pipe = [NSPipe pipe];
    NSTask *t = [self backupsTask:@[@"list"] pipe:pipe];
    NSError *err = nil;
    NSDictionary *res = nil;
    if ([t launchAndReturnError:&err]) {
        NSData *d = [pipe.fileHandleForReading readDataToEndOfFile];
        [t waitUntilExit];
        res = [self parseJSON:d];
    }
    NSArray *items = [res[@"backups"] isKindOfClass:[NSArray class]] ? res[@"backups"] : @[];
    self.backups = items;
    [self.backupsTable reloadData];
    NSString *notice = [res[@"last_failure"] isKindOfClass:[NSString class]] ? res[@"last_failure"] : nil;
    self.backupsNotice.stringValue = notice ?: @"";
    self.backupsNotice.hidden = notice == nil;
    if (!res) self.backupsStatus.stringValue = @"Couldn't read the backups list; see the log.";
    else if (!items.count) self.backupsStatus.stringValue = @"No backups yet: one is taken every time you quit the game.";
    NSMutableArray *rows = [NSMutableArray array];
    for (NSDictionary *b in items)
        [rows addObject:@{@"id": b[@"id"] ?: @"", @"date": b[@"date_label"] ?: @"", @"kind": b[@"kind"] ?: @"",
                          @"kind_label": b[@"kind_label"] ?: @"", @"characters": b[@"characters_label"] ?: @""}];
    [self record:@{@"kind": @"backups_list", @"ok": @(res != nil && [res[@"ok"] boolValue]), @"rows": rows,
                   @"notice": notice ?: [NSNull null], @"list_exit": @(t.isRunning ? -1 : t.terminationStatus)}];
    if (notice) [self record:@{@"kind": @"notice", @"text": notice, @"where": @"backups"}];
    [self updateBackupButtons];
}

- (NSButton *)button:(NSString *)title action:(SEL)sel frame:(NSRect)r {
    NSButton *b = [NSButton buttonWithTitle:title target:self action:sel];
    b.frame = r;
    return b;
}

- (void)showBackups {
    NSRect r = NSMakeRect(0, 0, 680, 440);
    NSWindow *w = [[NSWindow alloc] initWithContentRect:r
                                              styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                                        NSWindowStyleMaskMiniaturizable
                                                backing:NSBackingStoreBuffered defer:NO];
    w.title = @"Moonglade – Backups";
    w.releasedWhenClosed = NO;
    w.delegate = self;
    HSBackgroundView *root = [[HSBackgroundView alloc] initWithFrame:r];
    w.contentView = root;
    NSTextField *title = [NSTextField labelWithString:@"Your save backups"];
    title.font = [NSFont boldSystemFontOfSize:16];
    title.frame = NSMakeRect(20, 402, 640, 24);
    [root addSubview:title];
    NSTextField *about = [NSTextField wrappingLabelWithString:
        @"Moonglade saves a backup every time you quit the game (the last 10 are kept). Restoring one rewinds your "
        @"characters, your companions and the AI players to that moment, and puts back your key bindings, macros "
        @"and chat settings from then. Before it does, it saves a safety backup of how things are now, so a restore "
        @"can always be undone."];
    about.font = [NSFont systemFontOfSize:12];
    about.frame = NSMakeRect(20, 340, 640, 56);
    [root addSubview:about];
    NSTextField *notice = [NSTextField wrappingLabelWithString:@""];
    notice.font = [NSFont systemFontOfSize:12];
    notice.textColor = [NSColor systemOrangeColor];
    notice.frame = NSMakeRect(20, 312, 640, 28);
    notice.hidden = YES;
    [root addSubview:notice];
    self.backupsNotice = notice;

    NSScrollView *sv = [[NSScrollView alloc] initWithFrame:NSMakeRect(20, 92, 640, 216)];
    sv.hasVerticalScroller = YES;
    sv.borderType = NSBezelBorder;
    NSTableView *tv = [[NSTableView alloc] initWithFrame:sv.bounds];
    NSArray *cols = @[@[@"date", @"When", @200], @[@"kind", @"Kind", @160], @[@"chars", @"Characters", @260]];
    for (NSArray *c in cols) {
        NSTableColumn *col = [[NSTableColumn alloc] initWithIdentifier:c[0]];
        col.title = c[1];
        col.width = [c[2] doubleValue];
        [tv addTableColumn:col];
    }
    tv.dataSource = self;
    tv.delegate = self;
    tv.allowsMultipleSelection = NO;
    tv.usesAlternatingRowBackgroundColors = YES;
    sv.documentView = tv;
    [root addSubview:sv];
    self.backupsTable = tv;

    NSTextField *status = [NSTextField wrappingLabelWithString:@""];
    status.font = [NSFont systemFontOfSize:11];
    status.textColor = [NSColor secondaryLabelColor];
    status.frame = NSMakeRect(20, 56, 640, 30);
    [root addSubview:status];
    self.backupsStatus = status;
    self.restoreButton = [self button:@"Restore…" action:@selector(restorePressed:) frame:NSMakeRect(20, 16, 120, 32)];
    self.quitButton = [self button:@"Quit" action:@selector(quitPressed:) frame:NSMakeRect(420, 16, 110, 32)];
    self.playButton = [self button:@"Play" action:@selector(playPressed:) frame:NSMakeRect(540, 16, 120, 32)];
    self.playButton.keyEquivalent = @"\r";
    for (NSButton *b in @[self.restoreButton, self.quitButton, self.playButton]) [root addSubview:b];
    self.backupsWindow = w;
    [w center];
    [self loadBackups];
    [NSApp activateIgnoringOtherApps:YES];
    [w makeKeyAndOrderFront:nil];
    [self recordBackupsShown:0];
}

- (BOOL)backupsWindowOnScreen {
    NSWindow *w = self.backupsWindow;
    if (!w || w.windowNumber <= 0) return NO;
    CFArrayRef list = CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, (CGWindowID)w.windowNumber);
    if (!list) return NO;
    BOOL on = NO;
    for (NSDictionary *x in (__bridge NSArray *)list)
        if ([x[(id)kCGWindowNumber] intValue] == w.windowNumber) on = [x[(id)kCGWindowIsOnscreen] boolValue];
    CFRelease(list);
    return on;
}

// "shown" once the window server has the window on screen (or after 1 s), then the scripted steps (test) start
- (void)recordBackupsShown:(int)attempt {
    BOOL on = [self backupsWindowOnScreen];
    if (!on && attempt < 20) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.05 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [self recordBackupsShown:attempt + 1];
        });
        return;
    }
    [self record:@{@"kind": @"backups_window", @"state": @"shown", @"visible": @(self.backupsWindow.visible),
                   @"onscreen": @(on), @"title": self.backupsWindow.title}];
    [self nextScriptStep];
}

- (NSInteger)numberOfRowsInTableView:(NSTableView *)tv { return (NSInteger)self.backups.count; }

- (NSView *)tableView:(NSTableView *)tv viewForTableColumn:(NSTableColumn *)col row:(NSInteger)row {
    NSTextField *cell = [tv makeViewWithIdentifier:col.identifier owner:self];
    if (!cell) {
        cell = [NSTextField labelWithString:@""];
        cell.identifier = col.identifier;
        cell.lineBreakMode = NSLineBreakByTruncatingTail;
    }
    NSDictionary *b = self.backups[(NSUInteger)row];
    NSString *key = [col.identifier isEqual:@"date"] ? @"date_label"
                  : [col.identifier isEqual:@"kind"] ? @"kind_label" : @"characters_label";
    cell.stringValue = [b[key] isKindOfClass:[NSString class]] ? b[key] : @"";
    cell.toolTip = cell.stringValue;
    return cell;
}

- (void)tableViewSelectionDidChange:(NSNotification *)n { [self updateBackupButtons]; }

- (void)updateBackupButtons {
    BOOL busy = self.restoreTask.running;
    self.restoreButton.enabled = !busy && self.backupsTable.selectedRow >= 0;
    self.playButton.enabled = !busy;
    self.quitButton.enabled = !busy;
}

- (NSDictionary *)selectedBackup {
    NSInteger row = self.backupsTable.selectedRow;
    return (row >= 0 && (NSUInteger)row < self.backups.count) ? self.backups[(NSUInteger)row] : nil;
}

- (void)restorePressed:(id)sender {
    NSDictionary *b = [self selectedBackup];
    if (!b || self.restoreTask.running) return;
    NSString *when = b[@"date_label"] ?: b[@"id"];
    NSString *title = [NSString stringWithFormat:@"Restore the backup from %@?", when];
    NSString *text = [NSString stringWithFormat:
        @"This rewinds your characters, your companions and the AI players to how they were on %@, and puts back "
        @"your key bindings, macros and chat settings from then. Anything played after that is undone.\n\n"
        @"A safety backup of how things are now is taken first, so you can undo this restore from this window.",
        when];
    BOOL yes;
    NSString *answerSource;
    if ([self testMode]) {
        NSString *ans = self.script.count ? self.script.firstObject : @"cancel";
        if (self.script.count) [self.script removeObjectAtIndex:0];
        yes = [ans isEqual:@"confirm"];
        answerSource = @"test script";
    } else {
        NSAlert *a = [NSAlert new];
        a.alertStyle = NSAlertStyleWarning;
        a.messageText = title;
        a.informativeText = text;
        [a addButtonWithTitle:@"Restore"];
        [a addButtonWithTitle:@"Cancel"];
        yes = [a runModal] == NSAlertFirstButtonReturn;
        answerSource = @"user";
    }
    [self record:@{@"kind": @"confirm", @"id": b[@"id"] ?: @"", @"title": title, @"text": text,
                   @"answer": yes ? @"restore" : @"cancel", @"source": answerSource}];
    if (!yes) {
        self.backupsStatus.stringValue = @"Restore cancelled; nothing was changed.";
        [self nextScriptStep];
        return;
    }
    [self startRestore:b];
}

- (void)startRestore:(NSDictionary *)b {
    NSPipe *pipe = [NSPipe pipe];
    NSMutableArray *rargs = [NSMutableArray arrayWithObjects:@"restore", b[@"id"], nil];
    if (self.testRestoreTarget) [rargs addObjectsFromArray:@[@"--target", self.testRestoreTarget]];
    NSTask *t = [self backupsTask:rargs pipe:pipe];
    NSMutableData *out = [NSMutableData data];
    pipe.fileHandleForReading.readabilityHandler = ^(NSFileHandle *h) {
        NSData *d = h.availableData;
        @synchronized (out) { [out appendData:d]; }
    };
    __weak HSLauncher *weakSelf = self;
    t.terminationHandler = ^(NSTask *task) {
        dispatch_async(dispatch_get_main_queue(), ^{
            pipe.fileHandleForReading.readabilityHandler = nil;
            NSData *rest = [pipe.fileHandleForReading readDataToEndOfFile];
            NSData *all;
            @synchronized (out) { [out appendData:rest]; all = [out copy]; }
            [weakSelf restoreFinished:task output:all backup:b];
        });
    };
    self.backupsStatus.stringValue = [NSString stringWithFormat:@"Restoring the backup from %@… (saving a safety "
                                      @"backup first; this takes about a minute)", b[@"date_label"] ?: b[@"id"]];
    NSError *err = nil;
    if (![t launchAndReturnError:&err]) {
        self.backupsStatus.stringValue = [NSString stringWithFormat:@"Couldn't start the restore: %@", err.localizedDescription];
        [self record:@{@"kind": @"restore", @"state": @"error", @"error": err.localizedDescription ?: @""}];
        return;
    }
    self.restoreTask = t;
    [self record:@{@"kind": @"restore", @"state": @"start", @"id": b[@"id"] ?: @"", @"task_pid": @(t.processIdentifier)}];
    [self updateBackupButtons];
}

- (void)restoreFinished:(NSTask *)task output:(NSData *)data backup:(NSDictionary *)b {
    NSDictionary *res = [self parseJSON:data];
    int code = task.terminationStatus;
    BOOL ok = code == 0 && [res[@"ok"] boolValue];
    NSString *safety = [res[@"safety"] isKindOfClass:[NSDictionary class]] ? res[@"safety"][@"id"] : nil;
    NSString *err = [res[@"error"] isKindOfClass:[NSString class]] ? res[@"error"] : @"see the log";
    [self record:@{@"kind": @"restore", @"state": @"done", @"id": b[@"id"] ?: @"", @"code": @(code), @"ok": @(ok),
                   @"safety": safety ?: [NSNull null], @"error": ok ? [NSNull null] : err}];
    self.restoreTask = nil;
    [self loadBackups];
    self.backupsStatus.stringValue = ok
        ? [NSString stringWithFormat:@"Restored. Your save is now from %@. The safety backup of how things were is "
                                     @"in the list (Before restore) if you want to undo it. Press Play to play.",
                                     b[@"date_label"] ?: b[@"id"]]
        : [NSString stringWithFormat:@"The restore didn't happen: %@%@", err, [err hasSuffix:@"."] ? @"" : @"."];
    [self record:@{@"kind": @"backups_status", @"after": @"restore", @"ok": @(ok), @"text": self.backupsStatus.stringValue,
                   @"visible": @(self.backupsWindow.visible), @"onscreen": @([self backupsWindowOnScreen])}];
    if (!ok && ![self testMode]) {
        NSAlert *a = [NSAlert new];
        a.alertStyle = NSAlertStyleWarning;
        a.messageText = @"The restore didn't happen";
        a.informativeText = err;
        [a runModal];
    }
    [self updateBackupButtons];
    [self nextScriptStep];
}

- (void)playPressed:(id)sender {
    if (self.restoreTask.running) return;
    [self record:@{@"kind": @"backups_window", @"state": @"closed", @"why": @"play"}];
    self.leavingBackups = YES;
    [self.backupsWindow close];
    self.backupsWindow = nil;
    [self startPlay];
}

- (void)quitPressed:(id)sender {
    if (self.restoreTask.running) return;
    [self.backupsWindow close];                        // windowWillClose: records and exits 0
}

- (void)windowWillClose:(NSNotification *)n {
    if (n.object != self.backupsWindow || self.leavingBackups) return;
    [self record:@{@"kind": @"backups_window", @"state": @"closed", @"why": @"closed"}];
    [self finish:0];
}

- (BOOL)windowShouldClose:(NSWindow *)w {
    return !(w == self.backupsWindow && self.restoreTask.running);
}

// test-only: the next scripted click in the Backups window
- (void)nextScriptStep {
    if (![self testMode] || !self.script.count || !self.backupsWindow) return;
    NSString *step = self.script.firstObject;
    [self.script removeObjectAtIndex:0];
    [self record:@{@"kind": @"script_step", @"step": step}];
    if ([step hasPrefix:@"select:"]) {
        NSString *bid = [step substringFromIndex:7];
        NSUInteger i = [self.backups indexOfObjectPassingTest:^BOOL(NSDictionary *b, NSUInteger idx, BOOL *stop) {
            return [b[@"id"] isEqual:bid];
        }];
        if (i != NSNotFound) [self.backupsTable selectRowIndexes:[NSIndexSet indexSetWithIndex:i] byExtendingSelection:NO];
        [self record:@{@"kind": @"backups_select", @"id": bid, @"found": @(i != NSNotFound)}];
        [self nextScriptStep];
    } else if ([step isEqual:@"restore"]) {
        [self restorePressed:nil];                     // consumes the confirm/cancel step itself
    } else if ([step hasPrefix:@"wait:"]) {
        [self waitForFile:[step substringFromIndex:5] since:[NSDate timeIntervalSinceReferenceDate]];
    } else if ([step isEqual:@"play"]) {
        [self playPressed:nil];
    } else if ([step isEqual:@"close"]) {
        [self.backupsWindow performClose:nil];
    } else {
        [self nextScriptStep];
    }
}

- (void)waitForFile:(NSString *)f since:(double)t0 {
    if ([[NSFileManager defaultManager] fileExistsAtPath:f] || [NSDate timeIntervalSinceReferenceDate] - t0 > 900) {
        [self nextScriptStep];
        return;
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.2 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [self waitForFile:f since:t0];
    });
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
