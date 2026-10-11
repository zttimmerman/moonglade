// mgtest.h: Moonglade's minimal C++ unit-test header (openspec/changes/test-pyramid design 4). No framework download.
//   MG_TEST(suite, name) { MG_EQ(a, b); MG_TRUE(cond); }
// The binary prints one line per test, "ok <suite.name> <ms>" or "not ok <suite.name> <ms>", with failure details on
// "# " lines before it, then "# tests N failed M". Exit status 1 when any test failed. `mg_unit <substring>` runs only
// the tests whose id contains it.
#ifndef MG_TEST_H
#define MG_TEST_H

#include <chrono>
#include <cstdio>
#include <cstring>
#include <sstream>
#include <string>
#include <vector>

namespace mgtest
{
    struct Case { const char* suite; const char* name; void (*fn)(); };
    inline std::vector<Case>& Cases() { static std::vector<Case> v; return v; }
    inline int& Failures() { static int n = 0; return n; }
    struct Reg { Reg(const char* s, const char* n, void (*f)()) { Cases().push_back({s, n, f}); } };

    template <typename T> std::string Show(T const& v) { std::ostringstream o; o << v; return o.str(); }
    inline std::string Show(std::string const& v) { return "\"" + v + "\""; }
    inline std::string Show(const char* v) { return v ? "\"" + std::string(v) + "\"" : "null"; }
    template <typename T> std::string Show(std::vector<T> const& v)
    {
        std::string s = "[";
        for (size_t i = 0; i < v.size(); ++i)
            s += (i ? ", " : "") + Show(v[i]);
        return s + "]";
    }

    inline void Fail(const char* file, int line, std::string const& what)
    {
        ++Failures();
        std::printf("# %s:%d: %s\n", file, line, what.c_str());
    }

    inline int RunAll(int argc, char** argv)
    {
        const char* filter = argc > 1 ? argv[1] : nullptr;
        int run = 0, failed = 0;
        for (Case const& c : Cases())
        {
            std::string id = std::string(c.suite) + "." + c.name;
            if (filter && id.find(filter) == std::string::npos)
                continue;
            int before = Failures();
            auto t0 = std::chrono::steady_clock::now();
            c.fn();
            double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
            bool ok = Failures() == before;
            ++run;
            failed += ok ? 0 : 1;
            std::printf("%s %s %.3f\n", ok ? "ok" : "not ok", id.c_str(), ms);
        }
        std::printf("# tests %d failed %d\n", run, failed);
        return failed ? 1 : 0;
    }
}

#define MG_CAT2(a, b) a##b
#define MG_CAT(a, b) MG_CAT2(a, b)
#define MG_TEST(suite, name)                                                                      \
    static void MG_CAT(mg_test_, MG_CAT(suite, MG_CAT(_, name)))();                               \
    static mgtest::Reg MG_CAT(mg_reg_, MG_CAT(suite, MG_CAT(_, name)))(#suite, #name,            \
                                                    &MG_CAT(mg_test_, MG_CAT(suite, MG_CAT(_, name)))); \
    static void MG_CAT(mg_test_, MG_CAT(suite, MG_CAT(_, name)))()
#define MG_EQ(a, b)                                                                               \
    do { auto const& mg_a = (a); auto const& mg_b = (b);                                          \
         if (!(mg_a == mg_b))                                                                     \
             mgtest::Fail(__FILE__, __LINE__, std::string(#a " == " #b ": got ") + mgtest::Show(mg_a) \
                          + " want " + mgtest::Show(mg_b)); } while (0)
#define MG_TRUE(c)                                                                                \
    do { if (!(c)) mgtest::Fail(__FILE__, __LINE__, "expected true: " #c); } while (0)

#endif
