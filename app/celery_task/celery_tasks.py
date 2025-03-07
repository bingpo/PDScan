from app.models import TaskModels
from app.utils import db,success_api, fail_api
from app.modules.tools import kill_xray_crawlergo, kill_httpx, kill_scaninfo, kill_oneforall
from app.modules import oneforall_module, httpx_module, scaninfo_module, xray_crawlergo_module
from PDScan import celery_app
from config import worker_task_list
import re
import traceback
import json
import time
import os
# python3 -m celery -A app.celery_task.celery_tasks worker --loglevel=info
# 创建Celery应用

def validate_task_id(string):
    pattern = r'^[a-fA-F0-9]{8}$'
    return re.match(pattern, string) is not None

# Celery任务：发送任务
# 通过task_status判断任务状态，如果为waiting，则将任务发送给worker
# 通过task_next_module判断下一步执行的模块
@celery_app.task
def send_task():
    # 查询所有task_status为waiting的任务
    waiting_tasks = TaskModels.query.filter_by(task_status='waiting').all()
    # 如果为空，则输出提示信息
    if not waiting_tasks:
        # print('[-] 定时任务：没有任务需要发送')
        return

    for task in waiting_tasks:
        # 判断task.task_next_module是否在worker_task_list中有对应模块，如果有，则发送任务
        if task.task_next_module not in worker_task_list.keys():
            print(f'[-] 任务ID：{task.task_id}，任务状态错误，未找到模块{task.task_next_module}')
            task.task_running_module = 'error'
            task.task_next_module = 'error'
            task.task_status = 'error'
            db.session.commit()
            continue

        for module_name in worker_task_list.keys():
            if task.task_next_module == module_name:
                try:
                    if worker_task_list[module_name] == "None":
                        print(f'[+] 无历史{module_name}任务，开启新{module_name}任务')
                        worker_task_list[module_name] = do_task.delay(module_name,task.task_id)
                        task.celery_task_id = worker_task_list[module_name].id
                        db.session.commit()

                        break
                    
                    # 不是PENDING状态的任务，说明其已经执行结束，或者执行出现问题，直接强制revoke，然后开启新任务
                    if worker_task_list[module_name].state == 'PENDING':
                        print(f'[-] {module_name}任务正在执行，等待下一轮任务分配')
                    else:
                        worker_task_list[module_name].revoke(terminate=True)
                        print(f'[+] 任务ID：{task.task_id}，开启{module_name}任务')
                        worker_task_list[module_name] = do_task.delay(module_name,task.task_id)
                        task.celery_task_id = worker_task_list[module_name].id
                        db.session.commit()
                    
                        

                except Exception as e:
                    print(f'[-] 任务ID：{task.task_id}，开启oneforall任务失败，错误信息：', str(e))       
                
    db.session.close()
    print('[+] 定时任务：所有任务已发送')
    return

# 自动分配模块
@celery_app.task(rate_limit='1/s')
def do_task(module_name,task_id):

    print(f'[+] 任务ID：{task_id}，开始执行{module_name}任务')

    try:
        # 查询任务
        task = TaskModels.query.filter_by(task_id=task_id).first()
        # 校验任务ID
        if not validate_task_id(task_id):
            print(f'[-] 任务ID：{task_id}，任务ID错误，含有非法字符')
            task.task_status = 'error'
            db.session.commit()
            return
        
        # 更新任务状态
        task.task_running_module = module_name
        task.task_status = 'running'
        db.session.commit()

        # 模拟任务执行
        # EVAL!!!!!!!!!!!!!
        # module_name已经写死，task_id也写死为自动生成的uuid
        module_func = f"{module_name}_module('{task_id}')"
        print(f'[+] module_func: {module_func}')
        eval(module_func)

        # task_next_module为task_module_list中自身模块名对应模块的下一项，先判断目前运行的模块是否为最后一项
        task_module_list = json.loads(task.task_module_list)
        if task_module_list.index(task.task_running_module) == len(task_module_list) - 1:
            task.task_status = 'finish'
            task.task_running_module = 'finish'
            task.task_next_module = 'finish'
            task.task_end_time = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime())
            db.session.commit()
            print(f'[+] 任务ID：{task_id}，{module_name}任务执行完成，全部任务完成，无下一步模块')
            return
        else:
            task.task_next_module = task_module_list[task_module_list.index(task.task_running_module) + 1]
            task.task_status = 'waiting'
            task.task_running_module = 'waiting'
            db.session.commit()
            print(f'[+] 任务ID：{task_id}，{module_name}任务执行完成，下一步执行模块：{task.task_next_module}')
        db.session.close()

    except Exception as e:
        task.task_status = 'error'
        db.session.commit()
        db.session.close()

        print(f'[-] 任务ID：{task_id}，{module_name}任务执行失败，错误信息:')
        print(traceback.format_exc())
        return
    
# 停止指定celery任务
@celery_app.task(rate_limit='1/s')
def stop_task():
    # 查询所有task_status为waiting_的任务
    waiting_tasks = TaskModels.query.filter_by(task_status='waiting_paused').all()
    # 如果为空，则输出提示信息
    if not waiting_tasks:
        # print('[-] 定时任务：没有任务需要暂停')
        return

    for task in waiting_tasks:
        if task.task_next_module not in worker_task_list.keys():
                print(f'[-] 任务ID：{task.task_id}，任务状态错误，未找到模块{task.task_next_module}')
                task.task_running_module = 'error'
                task.task_next_module = 'error'
                task.task_status = 'error'
                db.session.commit()

                continue
        
        for module_name in worker_task_list.keys():
            if task.task_running_module == module_name:
                try:
                    worker_task_list[module_name].revoke(terminate=True)
                    # 动态调用kill函数
                    kill_func = f"kill_{module_name}()"
                    eval(kill_func)
                    print(f'任务ID：{task.task_id}，停止{module_name}任务成功')
                    task.task_status = 'paused'
                    # 因为在task_running_module运行时暂停的，所以下一步执行的模块应该是task_running_module
                    task.task_next_module = task.task_running_module
                    db.session.commit()
                    db.session.close()

                    return
                except Exception as e:
                    print(f'任务ID：{task.task_id}，停止{module_name}任务失败，错误信息：{str(e)}') 
                    task.task_status = 'error'
                    db.session.commit()
                    db.session.close()

                    return
                
