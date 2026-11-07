# client/sync-client.ps1
<#
.SYNOPSIS
    Script Unificado de Sincronizacao Zero-Trust (Project Zomboid).
.DESCRIPTION
    - Detecta instalacao limpa ou atualizacao automaticamente.
    - Aplica patch no ProjectZomboid64.json para carregar o ZombieBuddy.
    - Baixa apenas arquivos Java/DLL que mudaram de hash de forma granular.
    - Suporta -Uninstall para reverter mudancas.
#>

param(
    [string]$ManifestUrl = "https://raw.githubusercontent.com/Cadern0/JavaMods_zomboid_verified/main/manifest.json",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg, [string]$color = "Cyan") {
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg) -ForegroundColor $color
}

function Find-PzInstallation {
    $steamPath = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name "SteamPath" -ErrorAction SilentlyContinue).SteamPath
    if ($steamPath) {
        $candidate = Join-Path $steamPath "steamapps\common\ProjectZomboid"
        if (Test-Path $candidate) { return $candidate }
    }
    $defaultPath = "${env:ProgramFiles(x86)}\Steam\steamapps\common\ProjectZomboid"
    if (Test-Path $defaultPath) { return $defaultPath }
    throw "Pasta de instalacao do Project Zomboid nao localizada automaticamente."
}

function Assert-GameNotRunning {
    $processNames = @("ProjectZomboid64", "ProjectZomboid", "ProjectZomboid.exe", "ProjectZomboid64.exe")
    foreach ($name in $processNames) {
        $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
        if ($procs) {
            throw "O jogo esta em execucao ($name). Feche o Project Zomboid antes de executar este script."
        }
    }
}

function Resolve-PathSafe([string]$path, [string[]]$allowedRoots) {
    $full = [IO.Path]::GetFullPath($path)
    foreach ($root in $allowedRoots) {
        $rootFull = [IO.Path]::GetFullPath($root)
        if ($full.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
            return $full
        }
    }
    throw "Caminho de destino nao permitido: $full (fora de $($allowedRoots -join ', '))"
}

$gameDir = Find-PzInstallation
$userModsDir = Join-Path $env:USERPROFILE "Zomboid\mods"
Write-Step "Project Zomboid localizado em: $gameDir" "Green"

$jsonConfigPath = Join-Path $gameDir "ProjectZomboid64.json"
$backupPath = "$jsonConfigPath.bak"

if ($Uninstall) {
    Write-Step "Modo desinstalacao solicitado." "Yellow"
    Assert-GameNotRunning

    # Restaurar JSON do backup
    if (Test-Path $backupPath) {
        Write-Step "Restaurando ProjectZomboid64.json do backup..." "Yellow"
        Copy-Item $backupPath $jsonConfigPath -Force
        Write-Step "JSON restaurado." "Green"
    } else {
        Write-Step "Backup nao encontrado; removendo classpath do ZombieBuddy se presente..." "Yellow"
        if (Test-Path $jsonConfigPath) {
            $rawJson = Get-Content $jsonConfigPath -Raw | ConvertFrom-Json
            $classPathList = @($rawJson.classPath) | Where-Object { $_ -and $_ -notlike "*ZombieBuddy*" }
            $rawJson.classPath = $classPathList
            $rawJson | ConvertTo-Json -Depth 10 | Set-Content $jsonConfigPath -Encoding UTF8
            Write-Step "Classpath limpo." "Green"
        }
    }

    # Remover binarios do ZombieBuddy
    $removed = 0
    foreach ($file in @("ZombieBuddy.jar", "zbNative.dll", "java\ZombieBuddy.jar")) {
        $target = Resolve-PathSafe (Join-Path $gameDir $file), @($gameDir)
        if (Test-Path $target) {
            Remove-Item $target -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path $target)) {
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

# Verificar se o jogo esta fechado antes de modificar arquivos
Assert-GameNotRunning

# Patch Idempotente do ZombieBuddy (criar backup apenas se nao existir)
if (Test-Path $jsonConfigPath) {
    $rawJson = Get-Content $jsonConfigPath -Raw | ConvertFrom-Json
    # Garantir que classPath seja array sem nulos
    $classPathList = [System.Collections.ArrayList]@(@($rawJson.classPath) | Where-Object { $_ })

    if (-not ($classPathList -contains "java/ZombieBuddy.jar")) {
        Write-Step "Configurando ZombieBuddy no executavel do jogo (Primeira Execucao)..." "Yellow"
        if (-not (Test-Path $backupPath)) {
            Copy-Item $jsonConfigPath $backupPath -Force
            Write-Step "Backup do ProjectZomboid64.json criado." "DarkGray"
        }
        $classPathList.Add("java/ZombieBuddy.jar") | Out-Null
        $rawJson.classPath = $classPathList
        $rawJson | ConvertTo-Json -Depth 10 | Set-Content $jsonConfigPath -Encoding UTF8
        Write-Step "Injecao concluida com backup gerado!" "Green"
    } else {
        Write-Step "Injecao do ZombieBuddy ja configurada no ProjectZomboid64.json." "DarkGray"
    }
}

# Download e Consulta do Manifesto
Write-Step "Consultando manifesto seguro do servidor..." "Cyan"
try {
    $manifest = (Invoke-RestMethod -Uri $ManifestUrl -UseBasicParsing)
} catch {
    Write-Step "Nao foi possivel baixar o manifesto do servidor: $_" "Red"
    exit 1
}

# Sincronizacao Granular
$categories = @($manifest.engine_core, $manifest.java_mods)
$downloadCount = 0

foreach ($cat in $categories) {
    if (-not $cat) { continue }
    foreach ($modProp in $cat.PSObject.Properties) {
        $modName = $modProp.Name
        $modData = $modProp.Value

        foreach ($file in $modData.files) {
            $destPath = $file.target_path.Replace("%GAME_DIR%", $gameDir).Replace("%USER_MODS%", $userModsDir)
            $destFolder = Split-Path $destPath -Parent

            if (-not (Test-Path $destFolder)) {
                New-Item -ItemType Directory -Path $destFolder -Force | Out-Null
            }

            # Validar que o destino esta dentro dos diretorios permitidos
            try {
                $destPath = Resolve-PathSafe $destPath, @($gameDir, $userModsDir)
            } catch {
                Write-Step "Caminho invalido para $($file.name): $_" "Red"
                exit 1
            }

            $needsDownload = $true
            if (Test-Path $destPath) {
                $currentHash = (Get-FileHash -Path $destPath -Algorithm SHA256).Hash.ToLower()
                if ($currentHash -eq $file.sha256.ToLower()) {
                    $needsDownload = $false
                    Write-Step "[$modName] $($file.name) ja esta atualizado e integro." "DarkGray"
                }
            }

            if ($needsDownload) {
                Write-Step "[$modName] Baixando versao homologada de $($file.name)..." "Yellow"
                $tmpPath = "$destPath.tmp"
                try {
                    Invoke-WebRequest -Uri $file.url -OutFile $tmpPath -UseBasicParsing
                } catch {
                    Write-Step "Falha no download de $($file.name): $_" "Red"
                    Remove-Item $tmpPath -Force -ErrorAction SilentlyContinue
                    exit 1
                }

                $newHash = (Get-FileHash -Path $tmpPath -Algorithm SHA256).Hash.ToLower()
                if ($newHash -ne $file.sha256.ToLower()) {
                    Remove-Item $tmpPath -Force -ErrorAction SilentlyContinue
                    Write-Step "FALHA CRITICA: O hash de $($file.name) nao confere com o manifesto!" "Red"
                    exit 1
                }

                Move-Item $tmpPath $destPath -Force
                $downloadCount++
                Write-Step "[$modName] $($file.name) validado com sucesso!" "Green"
            }
        }
    }
}

Write-Host ""
if ($downloadCount -eq 0) {
    Write-Step "Tudo em ordem! Nenhuma atualizacao necessaria." "Green"
} else {
    Write-Step "Sincronizacao finalizada! $downloadCount arquivo(s) foram atualizados." "Green"
}
Write-Step "Voce ja pode abrir o Project Zomboid e entrar no servidor com seguranca." "Green"