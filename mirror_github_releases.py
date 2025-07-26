import os
import json
import requests
import datetime
import time
import traceback
import subprocess
from github import Github, GithubException

# 环境变量与配置
SOURCE_REPO = os.environ['SOURCE_REPO']
TARGET_REPO = os.environ.get('TARGET_REPO', os.environ['GITHUB_REPOSITORY'])
GITHUB_TOKEN = os.environ['GITHUB_TOKEN']
SOURCE_GITHUB_TOKEN = os.environ.get('SOURCE_GITHUB_TOKEN', GITHUB_TOKEN)
SYNCED_DATA_FILE = os.environ.get('SYNCED_DATA_FILE', 'synced_data.json')
SYNCED_DATA_BACKUP = f"{SYNCED_DATA_FILE}.bak"
SOURCE_OWNER, SOURCE_REPO_NAME = SOURCE_REPO.split('/')
RETRY_COUNT = int(os.environ.get('RETRY_COUNT', 3))
RETRY_DELAY = int(os.environ.get('RETRY_DELAY', 10))

print(f"=== 配置信息 ===")
print(f"源仓库: {SOURCE_REPO}")
print(f"目标仓库: {TARGET_REPO}")
print(f"配置: 仅当版本有文件更新时提交同步状态")


### 1. 同步状态文件管理
def load_synced_data():
    def _load(path):
        with open(path, 'r') as f:
            return json.load(f)
    
    try:
        if os.path.exists(SYNCED_DATA_FILE):
            return _load(SYNCED_DATA_FILE)
    except Exception as e:
        print(f"主文件损坏，尝试从备份恢复: {str(e)}")
        if os.path.exists(SYNCED_DATA_BACKUP):
            try:
                return _load(SYNCED_DATA_BACKUP)
            except Exception as e:
                print(f"备份文件也损坏: {str(e)}")
    
    return {'releases': {}, 'assets': {}, 'source_codes': {}}


def save_synced_data(data):
    temp_file = f"{SYNCED_DATA_FILE}.tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2)
        if os.path.exists(SYNCED_DATA_FILE):
            os.replace(SYNCED_DATA_FILE, SYNCED_DATA_BACKUP)
        os.replace(temp_file, SYNCED_DATA_FILE)
        print(f"同步状态已保存（含备份）")
    except Exception as e:
        print(f"保存失败: {str(e)}")
        if os.path.exists(temp_file):
            os.remove(temp_file)


### 2. 核心工具函数
def get_asset_info(asset):
    if not asset:
        return None
    updated_at = asset.updated_at.astimezone(datetime.timezone.utc) if asset.updated_at else None
    return {
        'size': asset.size,
        'updated_at': updated_at.isoformat() if updated_at else None
    }


def delete_existing_asset(target_release, asset_name):
    for asset in target_release.get_assets():
        if asset.name == asset_name:
            try:
                print(f"删除目标仓库中已存在的 {asset_name}")
                asset.delete_asset()
                return True
            except Exception as e:
                print(f"删除 {asset_name} 失败: {str(e)}")
    return False


def retry_upload(target_release, file_path, name, content_type):
    for attempt in range(RETRY_COUNT):
        try:
            delete_existing_asset(target_release, name)
            print(f"尝试上传 {name}（尝试 {attempt+1}/{RETRY_COUNT}）")
            uploaded_asset = target_release.upload_asset(
                file_path, name=name, content_type=content_type
            )
            if uploaded_asset:
                return uploaded_asset
            print(f"上传返回 None，重试中...")
        except GithubException as e:
            if e.status == 422:
                print(f"检测到文件冲突，强制删除后重试...")
                delete_existing_asset(target_release, name)
            else:
                print(f"上传失败: {str(e)}，{RETRY_DELAY} 秒后重试")
        except Exception as e:
            print(f"上传失败: {str(e)}，{RETRY_DELAY} 秒后重试")
        time.sleep(RETRY_DELAY)
    print(f"上传 {name} 达到最大重试次数，放弃")
    return None


### 3. 源代码同步（返回是否有文件更新）
def sync_source_code(tag_name, target_release, synced_data):
    if not target_release:
        print(f"错误：target_release 为 None，无法同步源代码 {tag_name}")
        return False
    
    print(f"\n===== 同步源代码: {tag_name} =====")
    source_files = {
        f"SourceCode_{tag_name}.zip": 
            f"https://github.com/{SOURCE_OWNER}/{SOURCE_REPO_NAME}/archive/refs/tags/{tag_name}.zip",
        f"SourceCode_{tag_name}.tar.gz": 
            f"https://github.com/{SOURCE_OWNER}/{SOURCE_REPO_NAME}/archive/refs/tags/{tag_name}.tar.gz"
    }
    existing_assets = {a.name: a for a in target_release.get_assets()}
    synced_data['source_codes'].setdefault(tag_name, {})
    has_changes = False  # 标记是否有文件更新
    
    for filename, url in source_files.items():
        if filename in existing_assets:
            print(f"目标仓库已存在 {filename}，跳过")
            if filename not in synced_data['source_codes'][tag_name]:
                synced_data['source_codes'][tag_name][filename] = {
                    'exists': True,
                    'synced_at': str(datetime.datetime.now())
                }
                save_synced_data(synced_data)
            continue
        
        # 目标不存在，需要同步（属于更新）
        print(f"目标仓库缺失 {filename}，开始同步")
        temp_path = f"temp_{filename}"
        try:
            download_file(url, temp_path)
            uploaded_asset = retry_upload(
                target_release, temp_path, filename, "application/zip"
            )
            
            if uploaded_asset:
                synced_data['source_codes'][tag_name][filename] = {
                    'exists': True,
                    'synced_at': str(datetime.datetime.now())
                }
                save_synced_data(synced_data)
                print(f"同步成功 {filename}")
                has_changes = True  # 标记有更新
            else:
                print(f"同步 {filename} 失败")
        except Exception as e:
            print(f"处理 {filename} 失败: {str(e)}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    print(f"===== 源代码同步完成: {tag_name} =====")
    return has_changes  # 返回是否有更新


### 4. Release附件同步（返回是否有文件更新）
def sync_release_assets(source_release, target_release, synced_data):
    source_id = str(source_release.id)
    source_assets = list(source_release.get_assets())
    target_assets = {a.name: a for a in target_release.get_assets()}
    synced_data['assets'].setdefault(source_id, {})
    has_changes = False  # 标记是否有文件更新
    
    print(f"\n===== 同步附件（{len(source_assets)} 个）: {source_release.tag_name} =====")
    for asset in source_assets:
        asset_name = asset.name
        asset_key = f"{asset_name}_{asset.size}"
        content_type = asset.content_type or "application/octet-stream"
        
        source_updated_at = asset.updated_at.astimezone(datetime.timezone.utc) if asset.updated_at else None
        source_info = {
            'size': asset.size,
            'updated_at': source_updated_at.isoformat() if source_updated_at else None
        }
        print(f"源文件 {asset_name} 信息: 大小={source_info['size']}B，时间={source_info['updated_at']}")
        
        need_sync = False
        target_asset = target_assets.get(asset_name)
        target_info = get_asset_info(target_asset)
        
        if asset_key not in synced_data['assets'][source_id]:
            need_sync = True
            print(f"本地记录缺失 {asset_name}，需要同步")
        elif not target_asset:
            need_sync = True
            print(f"目标仓库缺失 {asset_name}，重新同步")
        else:
            if source_info['size'] != target_info['size']:
                need_sync = True
                print(f"大小不一致: 源={source_info['size']}B 目标={target_info['size']}B")
            elif source_info['updated_at'] and target_info['updated_at']:
                source_time = datetime.datetime.fromisoformat(source_info['updated_at']).timestamp()
                target_time = datetime.datetime.fromisoformat(target_info['updated_at']).timestamp()
                if source_time > target_time:
                    need_sync = True
                    print(f"源文件更新: 源={source_info['updated_at']} 目标={target_info['updated_at']}")
        
        if not need_sync:
            print(f"附件 {asset_name} 无需同步")
            continue
        
        # 执行同步（属于更新）
        temp_path = f"temp_{asset.id}_{asset_name}"
        try:
            download_file(asset.browser_download_url, temp_path)
            uploaded_asset = retry_upload(
                target_release, temp_path, asset_name, content_type
            )
            
            if uploaded_asset:
                actual_info = get_asset_info(uploaded_asset)
                synced_data['assets'][source_id][asset_key] = {
                    'name': asset_name,
                    'size': actual_info['size'],
                    'updated_at': actual_info['updated_at'],
                    'synced_at': str(datetime.datetime.now())
                }
                save_synced_data(synced_data)
                print(f"同步成功 {asset_name}（大小={actual_info['size']}B，时间={actual_info['updated_at']}）")
                has_changes = True  # 标记有更新
            else:
                print(f"同步 {asset_name} 失败")
        except Exception as e:
            print(f"处理 {asset_name} 失败: {str(e)}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    print(f"===== 附件同步完成: {source_release.tag_name} =====")
    return has_changes  # 返回是否有更新


### 5. 辅助函数与主函数
def download_file(url, save_path):
    if os.path.exists(save_path):
        print(f"文件已存在: {save_path}，跳过下载")
        return save_path
    
    try:
        print(f"开始下载: {url}")
        resp = requests.get(url, stream=True, timeout=600)
        resp.raise_for_status()
        
        with open(save_path, 'wb') as f:
            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 8192
            
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded % (10 * 1024 * 1024) < chunk_size and total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"下载进度: {downloaded//(1024*1024):d}MB / {total_size//(1024*1024):d}MB ({percent:.1f}%)")
        
        print(f"下载成功: {save_path}（{os.path.getsize(save_path)} 字节）")
        return save_path
    except Exception as e:
        print(f"下载失败: {str(e)}")
        if os.path.exists(save_path):
            os.remove(save_path)
        raise


def get_or_create_release(target_repo, tag_name, name, body, draft, prerelease):
    release_name = name or tag_name
    print(f"查找 Release: {tag_name}")
    
    for release in target_repo.get_releases():
        if release.tag_name == tag_name:
            print(f"找到现有 Release: {tag_name}")
            return release
    
    print(f"创建新 Release: {tag_name}")
    try:
        try:
            target_repo.get_git_ref(f"tags/{tag_name}")
        except GithubException:
            default_branch = target_repo.default_branch
            print(f"创建 Tag: {tag_name} 基于 {default_branch}")
            target_repo.create_git_ref(
                ref=f"refs/tags/{tag_name}",
                sha=target_repo.get_branch(default_branch).commit.sha
            )
        
        release = target_repo.create_git_release(
            tag=tag_name, name=release_name, message=body or "", draft=draft, prerelease=prerelease
        )
        return release
    except Exception as e:
        print(f"创建 Release 失败: {str(e)}")
        for release in target_repo.get_releases():
            if release.tag_name == tag_name:
                print(f"找到现有 Release（第二轮查找）: {tag_name}")
                return release
        return None


def push_after_version(tag_name):
    """仅在有文件更新时提交"""
    try:
        subprocess.run(
            ['git', 'config', 'user.email', 'action@github.com'],
            check=True, capture_output=True, text=True
        )
        subprocess.run(
            ['git', 'config', 'user.name', 'GitHub Action'],
            check=True, capture_output=True, text=True
        )
        
        # 检查是否有文件变化
        status = subprocess.run(
            ['git', 'status', '--porcelain', SYNCED_DATA_FILE, SYNCED_DATA_BACKUP],
            capture_output=True, text=True
        ).stdout
        if not status:
            print(f"ℹ️ 版本 {tag_name} 无文件更新，无需提交")
            return
        
        # 有变化则提交
        subprocess.run(
            ['git', 'add', SYNCED_DATA_FILE, SYNCED_DATA_BACKUP],
            check=True, capture_output=True, text=True
        )
        commit_msg = f"版本 {tag_name} 有文件更新，同步状态"
        subprocess.run(
            ['git', 'commit', '-m', commit_msg],
            check=True, capture_output=True, text=True
        )
        subprocess.run(
            ['git', 'push'],
            check=True, capture_output=True, text=True
        )
        print(f"✅ 已提交版本 {tag_name} 的更新状态")
    
    except subprocess.CalledProcessError as e:
        print(f"⚠️ 提交版本 {tag_name} 失败: {e.stderr}")
    except Exception as e:
        print(f"⚠️ 提交过程异常: {str(e)}")


def main():
    synced_data = load_synced_data()
    source_github = Github(SOURCE_GITHUB_TOKEN)
    target_github = Github(GITHUB_TOKEN)
    
    try:
        source_repo = source_github.get_repo(SOURCE_REPO)
        target_repo = target_github.get_repo(TARGET_REPO)
        source_releases = sorted(source_repo.get_releases(), key=lambda r: r.created_at)
        print(f"发现 {len(source_releases)} 个 Release，开始处理...")
        
        for release in source_releases:
            tag_name = release.tag_name
            source_id = str(release.id)
            print(f"\n\n===== 开始处理 Release: {tag_name} =====")
            
            target_release = get_or_create_release(
                target_repo, tag_name, release.name, release.body, release.draft, release.prerelease
            )
            
            if not target_release:
                print(f"无法获取或创建 {tag_name}，跳过")
                continue
            
            # 同步并检查是否有文件更新
            code_changes = sync_source_code(tag_name, target_release, synced_data)
            asset_changes = sync_release_assets(release, target_release, synced_data)
            has_any_changes = code_changes or asset_changes  # 任意一项有更新即标记
            
            # 标记为完全同步
            synced_data['releases'][source_id] = {
                'tag_name': tag_name,
                'fully_synced_at': str(datetime.datetime.now())
            }
            save_synced_data(synced_data)
            
            # 仅当有文件更新时才提交
            if has_any_changes:
                print(f"\n===== 版本 {tag_name} 有文件更新，准备提交 =====")
                push_after_version(tag_name)
            else:
                print(f"\n===== 版本 {tag_name} 无文件更新，跳过提交 =====")
        
        print("\n===== 所有 Release 处理完成 =====")
        print(f"已同步 Release: {len(synced_data['releases'])}")
        print(f"已同步附件: {sum(len(v) for v in synced_data['assets'].values())}")
        print(f"已同步源代码: {sum(len(v) for v in synced_data['source_codes'].values())} 个文件")
    
    except Exception as e:
        print(f"全局错误: {str(e)}")
        traceback.print_exc()
    finally:
        for f in os.listdir('.'):
            if f.startswith('temp_'):
                os.remove(f)


if __name__ == "__main__":
    main()
