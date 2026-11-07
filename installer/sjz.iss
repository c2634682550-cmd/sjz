; SJZ-Control 安装器（Inno Setup 6）
; 构建：ISCC.exe installer\sjz.iss  （或在项目根跑 build.bat 的 installer 步骤）
;
; 设计要点：
;   · 安装目录由用户在向导里选，默认 {autopf}\SJZ-Control，不写死盘符
;   · 静默安装：Setup.exe /VERYSILENT /DIR="D:\Apps\SJZ-Control" [/TASKS="driver"]
;   · 卸载只删程序文件与快捷方式；配置/台账/截图/模板/许可证在卸载前被搬到
;     %LOCALAPPDATA%\SJZ-Control\uninstalled-<时间>，卸载页会显示具体位置
;   · 可选任务 driver 调用随包的 Interception 官方安装器（需重启）
;   · VC++ 2015-2022 x64 运行库缺失时提示，并可从 redist\vc_redist.x64.exe 安装

#define MyAppName "SJZ-Control"
#define MyAppPublisher "SJZ"
#define MyAppExeName "SJZ-Control.exe"
#define MyAppLauncher "launcher\SjzLauncher.exe"
; .NET 启动器两种形态都支持：runtime\pythonw.exe + desktop.py（旧）与 PyInstaller
; onedir（frozen，直接拉起 SJZ-Control.exe）。启动器随 dist\SJZ-Control\launcher\
; 一起打进安装包；万一缺了，快捷方式/图标回退到主 exe，避免指向不存在的文件
#define LauncherInDist FileExists(AddBackslash(SourcePath) + "..\dist\SJZ-Control\launcher\SjzLauncher.exe")
#include "version.iss"

[Setup]
AppId={{B2C7F4E1-6A3D-4E8F-9C21-5D7A1F0E8B33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\SJZ-Control
DisableDirPage=no
UsePreviousAppDir=yes
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
; 允许命令行覆盖：自动化/自助安装可走 /CURRENTUSER 装到用户目录，不必过 UAC
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=SJZ-Control-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
#if LauncherInDist
UninstallDisplayIcon={app}\{#MyAppLauncher}
#else
UninstallDisplayIcon={app}\{#MyAppExeName}
#endif
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#elif FileExists(AddBackslash(CompilerPath) + "Languages\Unofficial\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\Unofficial\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "driver"; Description: "安装高速输入驱动 Interception（内核驱动，需要重启，可稍后再装）"; GroupDescription: "可选组件："; Flags: unchecked

[Files]
; 整个 onedir 产物一起装（含 _internal、launcher、web、evo、tools、core\assets）
; Excludes：运行时生成的本机状态，绝不能打进安装包
Source: "..\dist\SJZ-Control\*"; DestDir: "{app}"; \
    Excludes: "config.json,config.json.good,config.json.lock,desktop.log,*.log"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; frozen onedir 把 tools/ 收进 _internal，安装后根目录还没有 tools（要等程序
; 首次启动时 _seed_missing_assets 才释放）。driver 任务在安装向导里就能勾选，
; 所以这里先把 Interception 官方安装器直接铺到 {app}\tools\interception。
Source: "..\dist\SJZ-Control\_internal\tools\interception\*"; \
    DestDir: "{app}\tools\interception"; Flags: ignoreversion
; VC++ 运行库：把 vc_redist.x64.exe 放到 installer\redist\ 即随安装器分发
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; \
    Flags: dontcopy skipifsourcedoesntexist
; 驱动排障手册：安装器/程序里的失败提示会指向 {app}\docs\driver-install.md
Source: "..\docs\driver-install.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppLauncher}"; \
    WorkingDir: "{app}"; Check: LauncherExists
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; Check: not LauncherExists
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppLauncher}"; \
    WorkingDir: "{app}"; Tasks: desktopicon; Check: LauncherExists
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; Tasks: desktopicon; Check: not LauncherExists

[Registry]
; 卸载信息由 Inno 自己写；这里只留一个安装位置键，卸载时一并删除
; HKA = 管理员安装写 HKLM，/CURRENTUSER 安装写 HKCU，避免非提权安装失败
Root: HKA; Subkey: "Software\SJZ-Control"; ValueType: string; \
    ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\SJZ-Control"; ValueType: string; \
    ValueName: "Version"; ValueData: "{#MyAppVersion}"; Flags: uninsdeletekey

[Run]
; 驱动安装在 [Code] 的 EnsureDriver 里做（要判幂等、要能拿到返回码并解释失败原因），
; 这里只留「装了驱动才出现」的重启勾选项。
Filename: "{sys}\shutdown.exe"; Parameters: "-r -t 10"; \
    Description: "立即重启电脑（内核驱动重启后生效）"; \
    Flags: postinstall nowait skipifsilent unchecked; Check: DriverJustInstalled
; 装完可立即启动（静默安装时跳过）
Filename: "{app}\{#MyAppLauncher}"; Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent; Check: LauncherExists
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent; Check: not LauncherExists

[UninstallDelete]
; 只清「运行时生成、安装清单里没有」的程序文件；用户数据已在 usUninstall 时搬走
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
Type: filesandordirs; Name: "{app}\core\__pycache__"
Type: filesandordirs; Name: "{app}\evo\__pycache__"
Type: files; Name: "{app}\config.json"
Type: files; Name: "{app}\config.json.good"
Type: files; Name: "{app}\config.json.lock"
Type: files; Name: "{app}\launcher.config.json"
Type: files; Name: "{app}\desktop.log"
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\templates"
Type: filesandordirs; Name: "{app}\logs"

[Code]
const
  UninstallKeepDir = 'uninstalled';

function LauncherExists: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\launcher\SjzLauncher.exe'));
end;

{ ---------- VC++ 运行库检测 ---------- }
function VCRedistInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result :=
    RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                       'Installed', Installed) and (Installed = 1);
  if not Result then
    Result :=
      RegQueryDWordValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                         'Installed', Installed) and (Installed = 1);
end;

procedure EnsureVCRedist;
var
  ErrCode: Integer;
begin
  if VCRedistInstalled then
    Exit;
  if not FileExists(ExpandConstant('{tmp}\vc_redist.x64.exe')) then
  begin
    MsgBox('检测到系统缺少 Microsoft Visual C++ 2015-2022 x64 运行库。' + #13#10 +
           '程序可能无法启动。请先安装 VC++ 运行库后重试。' + #13#10 +
           '（把 vc_redist.x64.exe 放进 installer\redist\ 重新打包，' +
           '安装器就能自动安装。）',
           mbInformation, MB_OK);
    Exit;
  end;
  if MsgBox('系统缺少 Microsoft Visual C++ 2015-2022 x64 运行库，是否现在安装？',
            mbConfirmation, MB_YESNO) = IDYES then
  begin
    ExtractTemporaryFile('vc_redist.x64.exe');
    if not Exec(ExpandConstant('{tmp}\vc_redist.x64.exe'), '/install /quiet /norestart',
                '', SW_SHOW, ewWaitUntilTerminated, ErrCode) or (ErrCode <> 0) then
      MsgBox('VC++ 运行库安装失败（错误码 ' + IntToStr(ErrCode) + '）。' + #13#10 +
             '可以稍后手动安装，程序启动时会再检查一次。', mbError, MB_OK);
  end;
end;

{ ---------- Interception 输入驱动 ---------- }
{ 事实：Interception 不是 interception.sys + 服务 interception，而是
  keyboard.sys / mouse.sys 两个上滤驱动 + 服务 keyboard / mouse。
  官方安装器把这些驱动内嵌在自己的资源段，随包分发即可，无需 .inf/.cat。 }
var
  DriverNeedsReboot: Boolean;

function DriverRegistered: Boolean;
begin
  Result := FileExists(ExpandConstant('{sys}\drivers\keyboard.sys')) and
            FileExists(ExpandConstant('{sys}\drivers\mouse.sys'));
  if not Result then
    Result := RegValueExists(HKLM, 'SYSTEM\CurrentControlSet\Services\keyboard', 'Start') and
              RegValueExists(HKLM, 'SYSTEM\CurrentControlSet\Services\mouse', 'Start');
end;

procedure EnsureDriver;
var
  Rc: Integer;
  Exe: String;
begin
  if not WizardIsTaskSelected('driver') then
    Exit;
  if DriverRegistered then
  begin
    { 幂等：已装就跳过，避免覆盖被内核锁住的 keyboard.sys 触发
      「Could not write to \system32\drivers」 }
    Log('Interception 驱动已在位，跳过安装');
    Exit;
  end;
  Exe := ExpandConstant('{app}\tools\interception\install-interception.exe');
  if not FileExists(Exe) then
  begin
    MsgBox('安装包内缺少驱动安装器：' + Exe + #13#10 +
           '请重新下载完整安装包，或联系技术支持。', mbError, MB_OK);
    Exit;
  end;
  WizardForm.StatusLabel.Caption := '正在安装 Interception 输入驱动（完成后需重启）...';
  if not Exec(Exe, '/install', '', SW_HIDE, ewWaitUntilTerminated, Rc) then
  begin
    MsgBox('驱动安装器无法启动（错误码 ' + IntToStr(Rc) + '）。' + #13#10 +
           '请以管理员身份手动运行：' + #13#10 + Exe + ' /install',
           mbError, MB_OK);
    Exit;
  end;
  if Rc <> 0 then
  begin
    if Rc = 1 then
      MsgBox('驱动安装失败（安装器返回 1：写入 System32\drivers 被系统拒绝）。' + #13#10 +
             '最常见原因是本机已装过、驱动正在运行，keyboard.sys/mouse.sys 被内核锁住；' + #13#10 +
             '这不是缺文件。处理：重启后重新运行安装程序并勾选『安装高速输入驱动』；' + #13#10 +
             '仍失败则先执行 sc stop keyboard 与 sc stop mouse 再装。详见 docs\driver-install.md。',
             mbError, MB_OK)
    else
      MsgBox('驱动安装失败（安装器返回码 ' + IntToStr(Rc) + '）。' + #13#10 +
             '详见安装目录 docs\driver-install.md，或手动执行：' + #13#10 +
             Exe + ' /install', mbError, MB_OK);
    Exit;
  end;
  DriverNeedsReboot := True;
  Log('Interception 驱动安装完成，需重启生效');
end;

function DriverJustInstalled: Boolean;
begin
  Result := DriverNeedsReboot;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    EnsureVCRedist;
    EnsureDriver;
  end;
end;

{ ---------- 卸载：保存用户数据（先验证后删） ---------- }
{ 硬约束：每次 CopyFile / CopyTree 之后都要校验目标存在且大小 / 数量一致。
  校验通过 → 本体交给 [UninstallDelete] 正常删除。
  校验失败 → 把本体改名搬出安装目录（rescue），绝不让卸载器静默删掉用户数据；
             搬不动就把失败详情写进
             %LOCALAPPDATA%\SJZ-Control\uninstall_preserve_error.log 并弹可读警告。 }
var
  PreserveLog: String;
  PreserveFailed: Boolean;
  PreserveFatal: Boolean;
  PreserveKeepDir: String;

procedure Plog(const S: String);
begin
  PreserveLog := PreserveLog + S + #13#10;
end;


function BoolStr(const B: Boolean): String;
begin
  if B then Result := 'true' else Result := 'false';
end;

{ 递归统计目录里的文件数与总字节数；目录读不了返回 False。
  Inno 里读文件大小用 FileSize(Name, Size: Integer)（1 参数形态不存在）。
  总字节数用 Integer 累加：同一棵树两次统计即使溢出也会同步回绕，不影响相等判断。 }
function TreeStats(const Dir: String; var Files, Bytes: Integer): Boolean;
var
  FindRec: TFindRec;
  F, B, SZ: Integer;
begin
  Files := 0;
  Bytes := 0;
  Result := True;
  if not FindFirst(Dir + '\*', FindRec) then
  begin
    Result := False;
    Exit;
  end;
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
      begin
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          if not TreeStats(Dir + '\' + FindRec.Name, F, B) then
            Result := False;
          Files := Files + F;
          Bytes := Bytes + B;
        end;
      end
      else
      begin
        Files := Files + 1;
        if FileSize(Dir + '\' + FindRec.Name, SZ) then
          Bytes := Bytes + SZ;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;
{ 单文件大小；不存在返回 False。 }
function StatFile(const Name: String; var Size: Integer): Boolean;
begin
  Size := 0;
  Result := FileSize(Name, Size);
end;
{ 递归复制 + 逐文件存在校验 + 整体数量/大小校验。 }
function CopyTreeVerified(const Src, Dst: String): Boolean;
var
  FindRec: TFindRec;
  SF, SB, DF, DB: Integer;
begin
  Result := True;
  if not DirExists(Dst) then
    if not ForceDirectories(Dst) then
    begin
      Plog('FAIL mkdir ' + Dst);
      Result := False;
      Exit;
    end;
  if not FindFirst(Src + '\*', FindRec) then
  begin
    Plog('FAIL read ' + Src);
    Result := False;
    Exit;
  end;
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
      begin
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          if not CopyTreeVerified(Src + '\' + FindRec.Name,
                                  Dst + '\' + FindRec.Name) then
            Result := False;
        end;
      end
      else
      begin
        if not CopyFile(Src + '\' + FindRec.Name, Dst + '\' + FindRec.Name, False) then
        begin
          Plog('FAIL copy ' + Src + '\' + FindRec.Name);
          Result := False;
        end
        else if not FileExists(Dst + '\' + FindRec.Name) then
        begin
          Plog('FAIL verify exist ' + Dst + '\' + FindRec.Name);
          Result := False;
        end;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
  if not TreeStats(Src, SF, SB) or not TreeStats(Dst, DF, DB)
     or (DF <> SF) or (DB <> SB) then
  begin
    Plog('FAIL verify totals ' + Src);
    Result := False;
  end;
end;

function CopyFileVerified(const Src, Dst: String): Boolean;
var
  SL, DL: Integer;
begin
  Result := False;
  if not CopyFile(Src, Dst, False) then
  begin
    Plog('FAIL copy ' + Src);
    Exit;
  end;
  if not FileExists(Dst) then
  begin
    Plog('FAIL verify exist ' + Dst);
    Exit;
  end;
  if not StatFile(Src, SL) or not StatFile(Dst, DL)
     or (SL <> DL) then
  begin
    Plog('FAIL verify size ' + Dst);
    Exit;
  end;
  Result := True;
end;

{ 拷贝失败时的兜底：把本体改名搬出安装目录，避免被 UninstallDelete 吃掉。
  先搬进保留目录；跨盘改名失败再搬到安装目录的上一级（同盘、且在安装目录之外）。 }
function RescueItem(const Src, Keep: String): Boolean;
var
  Dst, Alt: String;
begin
  Dst := Keep + '\rescue-' + ExtractFileName(Src);
  Result := RenameFile(Src, Dst);
  if Result then
  begin
    Plog('RESCUE ok ' + Src + ' -> ' + Dst);
    Exit;
  end;
  Alt := ExpandConstant('{app}\..\SJZ-Control-rescued-'
                        + GetDateTimeString('yyyymmdd-hhnnss', '-', ':') + '-'
                        + ExtractFileName(Src));
  Result := RenameFile(Src, Alt);
  if Result then
    Plog('RESCUE ok ' + Src + ' -> ' + Alt)
  else
    Plog('RESCUE FAILED ' + Src + '（本体可能被卸载器删除，请保留上面路径）');
end;

{ 保留目录清理：只留最近 3 份；删之前先确认这 3 份存在且非空（先验证后删）。 }
function PickMaxDir(const Base, Seen: String): String;
var
  FindRec: TFindRec;
begin
  Result := '';
  if not FindFirst(Base + '\' + UninstallKeepDir + '-*', FindRec) then
    Exit;
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
          if Pos('|' + FindRec.Name + '|', Seen) = 0 then
            if (Result = '') or (CompareText(FindRec.Name, Result) > 0) then
              Result := FindRec.Name;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function PickMinDir(const Base, Skip: String): String;
var
  FindRec: TFindRec;
begin
  Result := '';
  if not FindFirst(Base + '\' + UninstallKeepDir + '-*', FindRec) then
    Exit;
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
          if Pos('|' + FindRec.Name + '|', Skip) = 0 then
            if (Result = '') or (CompareText(FindRec.Name, Result) < 0) then
              Result := FindRec.Name;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure PruneOldKeep;
var
  Base, n1, n2, n3, oldest: String;
  F, B: Integer;
  guard: Integer;
begin
  Base := ExpandConstant('{localappdata}\SJZ-Control');
  n1 := PickMaxDir(Base, '');
  n2 := PickMaxDir(Base, '|' + n1 + '|');
  n3 := PickMaxDir(Base, '|' + n1 + '|' + n2 + '|');
  if (n1 = '') or (n2 = '') or (n3 = '') then
    Exit;
  { 先验证：要保留的 3 份必须存在且非空，否则这次不清理 }
  if not TreeStats(Base + '\' + n1, F, B) or (F <= 0) then Exit;
  if not TreeStats(Base + '\' + n2, F, B) or (F <= 0) then Exit;
  if not TreeStats(Base + '\' + n3, F, B) or (F <= 0) then Exit;
  guard := 0;
  while guard < 1000 do
  begin
    guard := guard + 1;
    oldest := PickMinDir(Base, '|' + n1 + '|' + n2 + '|' + n3 + '|');
    if oldest = '' then Exit;
    if DirExists(Base + '\' + oldest) then
    begin
      if not DelTree(Base + '\' + oldest, True, True, True) then
        Exit;
    end;
  end;
end;

procedure WritePreserveLog;
var
  Body, P: String;
begin
  Body := 'SJZ-Control uninstall preserve report' + #13#10
        + 'time: ' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + #13#10
        + 'keep: ' + PreserveKeepDir + #13#10
        + 'result: ' + BoolStr(not PreserveFailed) + #13#10
        + PreserveLog;
  P := ExpandConstant('{localappdata}\SJZ-Control\uninstall_preserve_error.log');
  if not SaveStringToFile(P, Body, False) then
  begin
    P := ExpandConstant('{tmp}\uninstall_preserve_error.log');
    SaveStringToFile(P, Body, False);
  end;
  if PreserveFailed then
    SaveStringToFile(ExpandConstant('{localappdata}\SJZ-Control\uninstall_preserve_failed'),
                     Body, False);
end;

function PreserveUserData: String;
var
  Keep, App, Lic: String;
begin
  PreserveLog := '';
  PreserveFailed := False;
  PreserveFatal := False;
  App := ExpandConstant('{app}');
  Lic := ExpandConstant('{localappdata}\SJZ-Control\license.json');
  Keep := ExpandConstant('{localappdata}\SJZ-Control\' + UninstallKeepDir + '-'
                         + GetDateTimeString('yyyymmdd-hhnnss', '-', ':'));
  PreserveKeepDir := Keep;
  Result := Keep;
  if not ForceDirectories(Keep) then
  begin
    Plog('FAIL mkdir keep dir ' + Keep);
    PreserveFailed := True;
  end
  else

  { 配置 }
  if FileExists(App + '\config.json') then
    if not CopyFileVerified(App + '\config.json', Keep + '\config.json') then
    begin
      PreserveFailed := True;
      if not RescueItem(App + '\config.json', Keep) then PreserveFatal := True;
    end;
  if FileExists(App + '\launcher.config.json') then
    if not CopyFileVerified(App + '\launcher.config.json', Keep + '\launcher.config.json') then
    begin
      PreserveFailed := True;
      if not RescueItem(App + '\launcher.config.json', Keep) then PreserveFatal := True;
    end;

  { 台账/截图 }
  if DirExists(App + '\data') then
    if not CopyTreeVerified(App + '\data', Keep + '\data') then
    begin
      PreserveFailed := True;
      if not RescueItem(App + '\data', Keep) then PreserveFatal := True;
    end;

  { 标定模板 }
  if DirExists(App + '\templates') then
    if not CopyTreeVerified(App + '\templates', Keep + '\templates') then
    begin
      PreserveFailed := True;
      if not RescueItem(App + '\templates', Keep) then PreserveFatal := True;
    end;

  { 许可证：本体留在 localappdata，另拷一份进保留目录，方便重装/迁移 }
  if FileExists(Lic) then
    if not CopyFileVerified(Lic, Keep + '\license.json') then
      PreserveFailed := True;       { 本体不动，拷副本失败只记日志 }
  PruneOldKeep;
  if PreserveFailed then
    WritePreserveLog;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  KeepPath: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    KeepPath := PreserveUserData;
    { 搬运失败且兜底也搬不动 → 直接中止卸载，宁可留着安装目录也不删用户数据。
      WritePreserveLog 已把失败详情落盘；静默卸载不弹窗，靠日志与非 0 退出码。 }
    if PreserveFatal then
    begin
      WritePreserveLog;
      if not UninstallSilent then
        MsgBox('用户数据搬运失败，已中止卸载以免误删。' + #13#10 +
               '失败详情见：' + #13#10 +
               ExpandConstant('{localappdata}\SJZ-Control\uninstall_preserve_error.log'),
               mbCriticalError, MB_OK);
      Abort;
    end;
    { 静默卸载（/SILENT、/VERYSILENT）不能弹窗，否则会卡死自动化 }
    if PreserveFailed then
    begin
      if not UninstallSilent then
        MsgBox('用户数据搬运失败：已尽量把原文件留在原处或搬到：' + #13#10 + KeepPath + #13#10#13#10 +
               '失败详情写入：' + #13#10 +
               ExpandConstant('{localappdata}\SJZ-Control\uninstall_preserve_error.log'),
               mbError, MB_OK);
    end
    else if not UninstallSilent then
      MsgBox('卸载将删除程序目录与快捷方式。' + #13#10#13#10 +
             '你的配置、台账、截图、标定模板、许可证不会被删除。' + #13#10 +
             '安装目录内的用户数据会先搬到：' + #13#10 + KeepPath + #13#10#13#10 +
             '许可证/日志等数据保留在：' + #13#10 +
             ExpandConstant('{localappdata}\SJZ-Control') + #13#10#13#10 +
             '如需彻底清除，卸载后手动删除上述目录。', mbInformation, MB_OK);
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if not UninstallSilent then
      MsgBox('已卸载。用户数据仍保留在：' + #13#10 +
             ExpandConstant('{localappdata}\SJZ-Control'), mbInformation, MB_OK);
  end;
end;
