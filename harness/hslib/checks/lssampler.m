// Test-only (launcher-fixes): one long-lived sampler of LaunchServices' view of running apps for app.single_dock_icon,
// replacing a subprocess (`lsappinfo list`) per sample, which starved under CPU load. Compiled by
// polish_checks.LsSampler into env/tools/ (never part of the app or the shim).
//
// usage: lssampler <period_ms> <max_seconds> <exe-prefix> <bundle-id>
// Prints one JSON line per sample: [epoch_seconds, [[pid, type, exe, bundle, name], ...]] for the apps whose
// executable starts with <exe-prefix> or whose bundle id is <bundle-id>. type is LaunchServices' name for the
// activation policy, as lsappinfo prints it: Foreground (regular, a Dock icon), UIElement (accessory),
// BackgroundOnly (prohibited). Between samples the run loop runs, so NSWorkspace's app list and each app's
// activation policy are current (they update from LaunchServices notifications delivered on the run loop).
#import <Cocoa/Cocoa.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 5) { fprintf(stderr, "usage: lssampler <period_ms> <max_s> <exe-prefix> <bundle-id>\n"); return 2; }
        double period = atof(argv[1]) / 1000.0, maxS = atof(argv[2]);
        NSString *prefix = @(argv[3]), *bundle = @(argv[4]);
        NSDate *end = [NSDate dateWithTimeIntervalSinceNow:maxS];
        NSFileHandle *out = [NSFileHandle fileHandleWithStandardOutput];
        setvbuf(stdout, NULL, _IONBF, 0);
        while ([end timeIntervalSinceNow] > 0) {
            @autoreleasepool {
                NSDate *next = [NSDate dateWithTimeIntervalSinceNow:period];
                double t = [[NSDate date] timeIntervalSince1970];
                NSMutableArray *rows = [NSMutableArray array];
                for (NSRunningApplication *a in [[NSWorkspace sharedWorkspace] runningApplications]) {
                    NSString *exe = a.executableURL.path ?: @"";
                    NSString *bid = a.bundleIdentifier ?: @"";
                    if (!([exe hasPrefix:prefix] || [bid isEqualToString:bundle])) continue;
                    NSString *type = a.activationPolicy == NSApplicationActivationPolicyRegular ? @"Foreground"
                                   : a.activationPolicy == NSApplicationActivationPolicyAccessory ? @"UIElement"
                                   : @"BackgroundOnly";
                    [rows addObject:@[@(a.processIdentifier), type, exe, bid, a.localizedName ?: @""]];
                }
                NSData *j = [NSJSONSerialization dataWithJSONObject:@[@(t), rows] options:0 error:nil];
                NSMutableData *line = [j mutableCopy];
                [line appendBytes:"\n" length:1];
                @try { [out writeData:line]; } @catch (NSException *e) { return 0; }   // reader gone
                [[NSRunLoop currentRunLoop] runUntilDate:next];
                [NSThread sleepUntilDate:next];          // the run loop may have nothing to wait on
            }
        }
    }
    return 0;
}
