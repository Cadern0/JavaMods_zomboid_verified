# client/sync-client.ps1
<#
.SYNOPSIS
    Script Unificado de Sincronizacao Zero-Trust (Project Zomboid).
.DESCRIPTION
    - Detecta instalacao limpa ou atualizacao automaticamente.
    - Baixa apenas arquivos Java/DLL que mudaram de hash de forma granular.
    - Aplica patch no ProjectZomboid64.json para carregar o ZombieBuddy
      (somente depois de o JAR estar baixado e validado).
    - Suporta -Uninstall para reverter mudancas.
#>

param(
    [string]$ManifestUrl = "https://raw.githubusercontent.com/Cadern0/JavaMods_zomboid_verified/main/manifest.json",
    [string]$GameDir = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
# PowerShell 5.1 pode negociar TLS antigo por padrao.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# Arquivos que o manifesto pode gravar na pasta do JOGO.
$GameDirAllowedExtensions = @(".jar", ".dll", ".so", ".dylib")
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Step([string]$msg, [string]$color = "Cyan") {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg) -ForegroundColor $color
}

function Find-PzInstallation {
    if ($GameDir) {
        if (Test-Path -LiteralPath (Join-Path $GameDir "ProjectZomboid64.json")) { return (Resolve-Path -LiteralPath $GameDir).Path }
        throw "ProjectZomboid64.json nao encontrado em -GameDir: $GameDir"
    }
    $steamPath = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name "SteamPath" -ErrorAction SilentlyContinue).SteamPath
    $roots = @()
    if ($steamPath) { $roots += $steamPath }
    $roots += "${env:ProgramFiles(x86)}\Steam"
    foreach ($root in $roots) {
        # Bibliotecas adicionais do Steam (libraryfolders.vdf)
        $vdf = Join-Path $root "steamapps\libraryfolders.vdf"
        $libraries = @($root)
        if (Test-Path -LiteralPath $vdf) {
            foreach ($m in [regex]::Matches((Get-Content -LiteralPath $vdf -Raw), '"path"\s+"([^"]+)"')) {
                $libraries += $m.Groups[1].Value.Replace("\\", "\")
            }
        }
        foreach ($lib in $libraries) {
            $candidate = Join-Path $lib "steamapps\common\ProjectZomboid"
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    throw "Pasta de instalacao do Project Zomboid nao localizada automaticamente. Use -GameDir."
}

function Assert-GameNotRunning {
    foreach ($name in @("ProjectZomboid64", "ProjectZomboid32", "ProjectZomboid")) {
        if (Get-Process -Name $name -ErrorAction SilentlyContinue) {
            throw "O jogo esta em execucao ($name). Feche o Project Zomboid antes de executar este script."
        }
    }
}

function Resolve-PathSafe {
    param([string]$Path, [string[]]$AllowedRoots)
    $full = [IO.Path]::GetFullPath($Path)
    foreach ($root in $AllowedRoots) {
        $rootFull = [IO.Path]::GetFullPath($root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        # Exige separador apos a raiz: "C:\Game" nao pode aceitar "C:\GameEvil".
        if ($full.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
            return $full
        }
    }
    throw "Caminho de destino nao permitido: $full (fora de $($AllowedRoots -join ', '))"
}

function Test-Within([string]$Path, [string]$Root) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return ([IO.Path]::GetFullPath($Path)).StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)
}

function Read-GameJson([string]$path) {
    $text = [IO.File]::ReadAllText($path)
    # Versoes antigas do sync-client.py criavam uma chave "classPath" ao lado
    # da chave real "classpath". O ConvertFrom-Json (que ignora maiusculas)
    # recusa chaves duplicadas, entao a chave invalida e removida antes.
    if ([regex]::IsMatch($text, '"classpath"\s*:') -and [regex]::IsMatch($text, '"classPath"\s*:')) {
        $text = [regex]::Replace($text, ',\s*"classPath"\s*:\s*\[[^\]]*\]', '')
        $text = [regex]::Replace($text, '"classPath"\s*:\s*\[[^\]]*\]\s*,', '')
    }
    return ($text | ConvertFrom-Json)
}

function Write-GameJson($data, [string]$path) {
    # Sem BOM: o Set-Content -Encoding UTF8 do PowerShell 5.1 grava BOM.
    $text = $data | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($path, $text + "`n", $Utf8NoBom)
}

function Test-ZbVmArg([string]$value) {
    $low = $value.Trim().ToLower()
    return ($low.StartsWith("-agentlib:zbnative") -or ($low.StartsWith("-javaagent:") -and $low.Contains("zombiebuddy")))
}

function Update-GameJsonZb([bool]$Enable) {
    # O ZombieBuddy e um agente Java: no Windows e ativado por -agentlib:zbNative
    # (o zbNative.dll carrega o ZombieBuddy.jar). Coloca-lo no classpath, como
    # versoes antigas faziam, nao ativa nada; essas entradas sao removidas.
    if (-not (Test-Path -LiteralPath $jsonConfigPath)) {
        Write-Step "ProjectZomboid64.json nao encontrado; configuracao ignorada." "Yellow"
        return
    }
    $original = [IO.File]::ReadAllText($jsonConfigPath)
    $hadDuplicateKey = [regex]::IsMatch($original, '"classpath"\s*:') -and [regex]::IsMatch($original, '"classPath"\s*:')
    $before = (Read-GameJson $jsonConfigPath | ConvertTo-Json -Depth 20)
    $rawJson = Read-GameJson $jsonConfigPath
    # Propriedades do PowerShell ignoram maiusculas: .classpath cobre "classpath".
    if ($rawJson.PSObject.Properties["classpath"]) {
        $rawJson.classpath = @(@($rawJson.classpath) | Where-Object { $_ -and ($_ -notlike "*ZombieBuddy*") })
    }
    $vmArgs = @(@($rawJson.vmArgs) | Where-Object { $_ -and -not (Test-ZbVmArg $_) })
    if ($Enable) { $vmArgs = @("-agentlib:zbNative") + $vmArgs }
    if ($rawJson.PSObject.Properties["vmArgs"]) {
        $rawJson.vmArgs = $vmArgs
    } else {
        $rawJson | Add-Member -NotePropertyName vmArgs -NotePropertyValue $vmArgs
    }
    $newText = ($rawJson | ConvertTo-Json -Depth 20)
    if ($newText -eq $before -and -not $hadDuplicateKey) {
        Write-Step "ProjectZomboid64.json ja esta correto." "DarkGray"
        return
    }
    if (-not (Test-Path -LiteralPath $backupPath)) {
        Copy-Item -LiteralPath $jsonConfigPath -Destination $backupPath -Force
        Write-Step "Backup do ProjectZomboid64.json criado." "DarkGray"
    }
    Write-GameJson $rawJson $jsonConfigPath
}

$gameDir = Find-PzInstallation
$userModsDir = Join-Path $env:USERPROFILE "Zomboid\mods"
Write-Step "Project Zomboid localizado em: $gameDir" "Green"

$jsonConfigPath = Join-Path $gameDir "ProjectZomboid64.json"
$backupPath = "$jsonConfigPath.bak"

if ($Uninstall) {
    Write-Step "Modo desinstalacao solicitado." "Yellow"
    Assert-GameNotRunning

    # Remocao cirurgica: restaurar o .bak inteiro desfaria atualizacoes do jogo.
    Update-GameJsonZb -Enable $false
    Write-Step "ZombieBuddy removido do ProjectZomboid64.json." "Green"

    $removed = 0
    foreach ($file in @("ZombieBuddy.jar", "zbNative.dll", "java\ZombieBuddy.jar")) {
        $target = Resolve-PathSafe -Path (Join-Path $gameDir $file) -AllowedRoots @($gameDir)
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $target)) {
                Write-Step "Removido: $file" "Green"
                $removed++
            }
        }
    }
    if ($removed -eq 0) {
        Write-Step "Nenhum arquivo do ZombieBuddy encontrado para remover." "DarkGray"
    }

    Write-Step "Desinstalacao concluida. O jogo voltara ao estado original." "Green"
    exit 0
}

if ($ManifestUrl -like "*{{*") {
    Write-Step "URL do manifesto nao configurada. Use -ManifestUrl https://..." "Red"
    exit 1
}
if (-not $ManifestUrl.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)) {
    Write-Step "A URL do manifesto precisa usar HTTPS: $ManifestUrl" "Red"
    exit 1
}

Assert-GameNotRunning

Write-Step "Consultando manifesto seguro do servidor..." "Cyan"
try {
    $manifest = Invoke-RestMethod -Uri $ManifestUrl -UseBasicParsing -Headers @{ "Cache-Control" = "no-cache" }
} catch {
    Write-Step "Nao foi possivel baixar o manifesto do servidor: $_" "Red"
    exit 1
}

$categories = @($manifest.engine_core, $manifest.java_mods)
$downloadCount = 0
$zombieBuddyReady = $false
$zbJarPath = $null

foreach ($cat in $categories) {
    if (-not $cat) { continue }
    foreach ($modProp in $cat.PSObject.Properties) {
        $modName = $modProp.Name
        foreach ($file in @($modProp.Value.files)) {
            if (-not $file) { continue }
            $name = [string]$file.name
            $sha = ([string]$file.sha256).Trim().ToLower()
            $url = [string]$file.url

            if ($sha -notmatch '^[0-9a-f]{64}$') {
                Write-Step "Hash SHA256 invalido para ${name}: $sha" "Red"
                exit 1
            }

            $raw = ([string]$file.target_path).Replace("/", "\")
            if ($raw -like "%GAME_DIR%\*") {
                $destPath = Join-Path $gameDir $raw.Substring(11)
            } elseif ($raw -like "%USER_MODS%\*") {
                $destPath = Join-Path $userModsDir $raw.Substring(12)
            } else {
                Write-Step "Caminho de destino nao reconhecido no manifesto: $raw" "Red"
                exit 1
            }

            # Valida ANTES de criar qualquer pasta
            try {
                $destPath = Resolve-PathSafe -Path $destPath -AllowedRoots @($gameDir, $userModsDir)
            } catch {
                Write-Step "Caminho invalido para ${name}: $_" "Red"
                exit 1
            }
            if ((Test-Within $destPath $gameDir) -and -not (Test-Within $destPath $userModsDir)) {
                $ext = [IO.Path]::GetExtension($destPath).ToLower()
                if ($GameDirAllowedExtensions -notcontains $ext) {
                    Write-Step "O manifesto so pode gravar $($GameDirAllowedExtensions -join ', ') na pasta do jogo: $destPath" "Red"
                    exit 1
                }
            }

            $destFolder = Split-Path $destPath -Parent
            if (-not (Test-Path -LiteralPath $destFolder)) {
                New-Item -ItemType Directory -Path $destFolder -Force | Out-Null
            }

            $needsDownload = $true
            if (Test-Path -LiteralPath $destPath) {
                $currentHash = (Get-FileHash -LiteralPath $destPath -Algorithm SHA256).Hash.ToLower()
                if ($currentHash -eq $sha) {
                    $needsDownload = $false
                    Write-Step "[$modName] $name ja esta atualizado e integro." "DarkGray"
                }
            }

            if ($needsDownload) {
                if (-not $url.StartsWith("https://", [StringComparison]::OrdinalIgnoreCase)) {
                    Write-Step "URL de download de $name precisa usar HTTPS: $url" "Red"
                    exit 1
                }
                Write-Step "[$modName] Baixando versao homologada de $name..." "Yellow"
                $tmpPath = "$destPath.tmp"
                try {
                    Invoke-WebRequest -Uri $url -OutFile $tmpPath -UseBasicParsing
                } catch {
                    Write-Step "Falha no download de ${name}: $_" "Red"
                    Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
                    exit 1
                }

                $newHash = (Get-FileHash -LiteralPath $tmpPath -Algorithm SHA256).Hash.ToLower()
                if ($newHash -ne $sha) {
                    Remove-Item -LiteralPath $tmpPath -Force -ErrorAction SilentlyContinue
                    Write-Step "FALHA CRITICA: O hash de $name nao confere com o manifesto!" "Red"
                    exit 1
                }

                Move-Item -LiteralPath $tmpPath -Destination $destPath -Force
                $downloadCount++
                Write-Step "[$modName] $name validado com sucesso!" "Green"
            }

            if ($name -ieq "ZombieBuddy.jar" -and (Test-Within $destPath $gameDir)) {
                $zombieBuddyReady = $true
                $zbJarPath = $destPath
            }
        }
    }
}

# Ativacao SOMENTE com os arquivos presentes e validados.
$activationFailed = $false
if ($zombieBuddyReady) {
    $rootJar = Join-Path $gameDir "ZombieBuddy.jar"
    # Manifestos antigos publicavam o JAR em java\; o agente precisa dele na raiz.
    if ($zbJarPath -and ([IO.Path]::GetFullPath($zbJarPath) -ne [IO.Path]::GetFullPath($rootJar))) {
        Write-Step "Manifesto antigo: copiando ZombieBuddy.jar para a raiz da pasta do jogo." "Yellow"
        Copy-Item -LiteralPath $zbJarPath -Destination $rootJar -Force
    }
    if (Test-Path -LiteralPath (Join-Path $gameDir "zbNative.dll")) {
        Update-GameJsonZb -Enable $true
        Write-Step "ZombieBuddy ativado (-agentlib:zbNative)." "Green"
    } else {
        # Limpa o metodo antigo, mas nao ativa: sem a DLL o agente nao carrega.
        Update-GameJsonZb -Enable $false
        Write-Step "zbNative.dll ausente: ele e obrigatorio no Windows para ativar o ZombieBuddy. Peca ao administrador do servidor para republicar incluindo o zbNative.dll." "Red"
        $activationFailed = $true
    }
}

Write-Host ""
if ($activationFailed) {
    Write-Step "Arquivos sincronizados, mas o ZombieBuddy NAO foi ativado (veja o erro acima)." "Red"
    exit 1
}
if ($downloadCount -eq 0) {
    Write-Step "Tudo em ordem! Nenhuma atualizacao necessaria." "Green"
} else {
    Write-Step "Sincronizacao finalizada! $downloadCount arquivo(s) foram atualizados." "Green"
}
Write-Step "Voce ja pode abrir o Project Zomboid e entrar no servidor com seguranca." "Green"
