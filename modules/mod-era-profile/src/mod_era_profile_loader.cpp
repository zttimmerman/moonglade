/*
 * mod-era-profile script loader. AzerothCore's modules/CMakeLists.txt calls Add<module dir, '-' -> '_'>Scripts()
 * for every static module (ConfigureScriptLoader), hence the name.
 */

void AddEraProfileScripts();

void Addmod_era_profileScripts()
{
    AddEraProfileScripts();
}
