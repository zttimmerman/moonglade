-- The Lua unit-test driver (openspec/changes/test-pyramid design 5), run under the pinned Lua 5.1.5:
--   lua run.lua <lua test dir> <repo root> <xml prelude> <pattern or ""> <test files...>
-- Each test file returns { name = function(T) ... end, ... }. T.boot(opts) gives a fresh stub world with the real
-- glue files loaded (see boot below). Prints "ok <file>.<name> <ms>" / "not ok <file>.<name> <ms>" (failure details
-- on "# " lines before it), then "# tests N failed M"; exits 1 when a test failed.

local dir, repo, prelude_path, pattern = arg[1], arg[2], arg[3], arg[4]
package.path = dir .. "/?.lua;" .. package.path
local S = require("wowstub")
local GLUE = repo .. "/client-mods/glue/"

local T = {}

local function fail(msg, level) error({ mgfail = msg }, (level or 1) + 1) end
function T.eq(got, want, what)
	if got ~= want then fail(string.format("%s: got %s want %s", what or "value", tostring(got), tostring(want)), 2) end
end
function T.truthy(v, what) if not v then fail("expected true: " .. (what or "?"), 2) end end

local function loadinto(path, env)
	local chunk, err = loadfile(path)
	if not chunk then error("loading " .. path .. ": " .. tostring(err)) end
	setfenv(chunk, env)
	return chunk()
end

-- boot{ profiles = {...}, default = "ACCOUNT", test = HS_TEST table or nil, saved = {accountName, accountList} }:
-- a fresh world; HSProfiles.lua is replaced by these values (the launcher generates it at each start), the other
-- <Script file> entries of HSFrontMenu.xml load unchanged in document order, then the XML frames are built.
function T.boot(opts)
	opts = opts or {}
	local w = S.new({ saved = opts.saved })
	-- the prelude's handler functions must see the world's globals, as the real scripts do
	local pre = loadinto(prelude_path, w.env)
	for _, f in ipairs(pre.scripts) do
		if f == "HSProfiles.lua" then
			w.env.HS_PROFILES = opts.profiles or {}
			w.env.HS_PROFILE_DEFAULT = opts.default
			w.env.HS_TEST = opts.test
			w.env.HS_CONTROL = nil
		else
			loadinto(GLUE .. f, w.env)
		end
	end
	pre.build(w)
	return w
end

-- boot_addon{ player = "Name", files = {...} }: a fresh in-game world (addonstub.lua) with the Moonglade add-on's
-- files (default: Moonglade.lua) loaded unchanged, as the client loads them from the TOC.
local ADDON = repo .. "/client-mods/addons/Moonglade/"
function T.boot_addon(opts)
	opts = opts or {}
	local A = require("addonstub")
	local w = A.new(opts)
	for _, f in ipairs(opts.files or { "Moonglade.lua" }) do loadinto(ADDON .. f, w.env) end
	return w
end

local files = {}
for i = 5, #arg do table.insert(files, arg[i]) end
local run, failed = 0, 0
for _, path in ipairs(files) do
	local base = path:match("([^/]+)%.lua$")
	local tests = dofile(path)
	local names = {}
	for n in pairs(tests) do table.insert(names, n) end
	table.sort(names)
	for _, n in ipairs(names) do
		local id = base .. "." .. n
		if pattern == "" or string.find(id, pattern, 1, true) then
			local t0 = os.clock()
			local ok, err = xpcall(function() tests[n](T) end, function(e)
				if type(e) == "table" and e.mgfail then return e.mgfail end
				local s = tostring(e)
				local api = s:match("attempt to call global '([^']+)' %(a nil value%)")
				if api then s = "unstubbed API: " .. api .. " (" .. s .. ")" end
				return s .. "\n" .. debug.traceback("", 2)
			end)
			local ms = (os.clock() - t0) * 1000
			run = run + 1
			if ok then
				print(string.format("ok %s %.3f", id, ms))
			else
				failed = failed + 1
				for line in (tostring(err) .. "\n"):gmatch("([^\n]*)\n") do print("# " .. line) end
				print(string.format("not ok %s %.3f", id, ms))
			end
		end
	end
end
print(string.format("# tests %d failed %d", run, failed))
os.exit(failed > 0 and 1 or 0)
