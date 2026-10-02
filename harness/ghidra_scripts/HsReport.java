// Read-only report script for `hs re` (ADR-0005). Writes findings to a text file; never modifies the program.
// Args: <mode> <outfile> [arg]   modes: entry | findascii <text> | decompile <addr> | xrefs <addr>
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.symbol.Reference;
import java.io.FileWriter;
import java.io.PrintWriter;

public class HsReport extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        String mode = a[0];
        try (PrintWriter out = new PrintWriter(new FileWriter(a[1], true))) {
            out.println("program\t" + currentProgram.getName() + "\t" + currentProgram.getExecutableSHA256());
            switch (mode) {
                case "entry": {
                    out.println("imagebase\t" + currentProgram.getImageBase());
                    AddressIterator it = currentProgram.getSymbolTable().getExternalEntryPointIterator();
                    while (it.hasNext()) {
                        out.println("entry\t" + it.next());
                    }
                    break;
                }
                case "findascii": {
                    byte[] pat = a[2].getBytes("US-ASCII");
                    Memory mem = currentProgram.getMemory();
                    Address start = mem.getMinAddress();
                    int n = 0;
                    while (start != null && n < 200) {
                        Address f = mem.findBytes(start, pat, null, true, monitor);
                        if (f == null) {
                            break;
                        }
                        out.println("match\t" + f + "\t" + cstring(f));
                        n++;
                        start = f.add(1);
                    }
                    out.println("matches\t" + n);
                    break;
                }
                case "decompile": {
                    Address addr = toAddr(a[2]);
                    Function fn = getFunctionAt(addr);
                    if (fn == null) {
                        fn = getFunctionContaining(addr);
                    }
                    if (fn == null) {
                        out.println("error\tno function at " + addr + " (run `hs re import --analyze` first)");
                        break;
                    }
                    DecompInterface d = new DecompInterface();
                    d.openProgram(currentProgram);
                    DecompileResults r = d.decompileFunction(fn, 120, monitor);
                    out.println("function\t" + fn.getName() + "\t" + fn.getEntryPoint());
                    out.println(r.decompileCompleted() ? r.getDecompiledFunction().getC() : "error\t" + r.getErrorMessage());
                    break;
                }
                case "xrefs": {
                    for (Reference r : getReferencesTo(toAddr(a[2]))) {
                        Function f = getFunctionContaining(r.getFromAddress());
                        out.println("xref\t" + r.getFromAddress() + "\t" + (f == null ? "" : f.getName() + "@" + f.getEntryPoint()));
                    }
                    break;
                }
                default:
                    out.println("error\tunknown mode " + mode);
            }
        }
    }

    private String cstring(Address at) {
        StringBuilder sb = new StringBuilder();
        try {
            for (int i = 0; i < 160; i++) {
                byte b = getByte(at.add(i));
                if (b == 0) {
                    break;
                }
                sb.append(b >= 32 && b < 127 ? (char) b : '.');
            }
        } catch (Exception e) {
            // end of block
        }
        return sb.toString();
    }
}
