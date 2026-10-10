/*
 * mod-moonglade script loader. AzerothCore's modules/CMakeLists.txt calls Add<module dir, '-' -> '_'>Scripts()
 * for every static module (ConfigureScriptLoader), hence the name.
 */

void AddMoongladeQuietScripts();
void AddMoongladeAddonScripts();
void AddMoongladePauseScripts();
void AddMoongladeSavesScripts();

void Addmod_moongladeScripts()
{
    AddMoongladeQuietScripts();
    AddMoongladeAddonScripts();
    AddMoongladePauseScripts();
    AddMoongladeSavesScripts();
}
