/* Moonglade release builds: a minimal stand-in for GNU readline (openspec/changes/release-build, design.md
 * "readline off"). It declares exactly what AzerothCore's worldserver console (CliRunnable.cpp) uses, so release
 * servers ship no GPL-3 readline. Line editing, history and tab completion are no-ops; release servers run with the
 * console unused. SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef MOONGLADE_READLINE_STUB_H
#define MOONGLADE_READLINE_STUB_H
#ifdef __cplusplus
extern "C" {
#endif
typedef char *rl_compentry_func_t(const char *, int);
typedef char **rl_completion_func_t(const char *, int, int);
typedef int rl_hook_func_t(void);
typedef int rl_command_func_t(int, int);

extern rl_completion_func_t *rl_attempted_completion_function;
extern int rl_attempted_completion_over;
extern char *rl_completer_word_break_characters;
extern rl_hook_func_t *rl_event_hook;
extern int rl_done;

char *readline(const char *prompt);
char **rl_completion_matches(const char *text, rl_compentry_func_t *entry);
int rl_bind_key(int key, rl_command_func_t *fn);
int rl_complete(int count, int key);
#ifdef __cplusplus
}
#endif
#endif
