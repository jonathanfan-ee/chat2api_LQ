import asyncio
import time
import types
import warnings

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, Response

from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens, verify_token, get_req_token
from chatgpt.authorization import refresh_all_tokens, verify_token, get_req_token
import chatgpt.globals as globals
from chatgpt.reverseProxy import chatgpt_reverse_proxy
from utils.Logger import logger
from utils.config import api_prefix, scheduled_refresh, authorization_list
from utils.config import api_prefix, scheduled_refresh, enable_gateway
from utils.retry import async_retry

warnings.filterwarnings("ignore")

app = FastAPI()
scheduler = AsyncIOScheduler()
templates = Jinja2Templates(directory="templates")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/4',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token):
    chat_service = ChatService(req_token)
    try:
        await chat_service.set_dynamic_data(request_data)
        await chat_service.get_chat_requirements()
        return chat_service
    except HTTPException as e:
        await chat_service.close_client()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


async def process(request_data, req_token):
    chat_service = await to_send_conversation(request_data, req_token)
    await chat_service.prepare_send_conversation()
    res = await chat_service.send_conversation()
    return chat_service, res


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, req_token: str = Depends(oauth2_scheme)):
    try:
        # 输出未进行提取的请求内容
        request_body = await request.body()
        logger.debug(f"Raw request body: {request_body.decode('utf-8')}")

        # 提取 JSON 数据并赋值给 request_data
        request_data = await request.json()

        # 获取模型名称
        model = request_data.get('model', '')
        o1_models = ['o1-mini', 'o1-preview']
        # 如果模型是 o1 系列，移除 system 消息
        if model in o1_models:
            original_length = len(request_data.get('messages', []))
            # 过滤掉 role 为 'system' 的消息
            filtered_messages = [msg for msg in request_data['messages'] if msg.get('role') != 'system']
            removed_count = original_length - len(filtered_messages)
            if removed_count > 0:
                request_data['messages'] = filtered_messages
                logger.info(f"Removed {removed_count} system message(s) for model '{model}'.")
                
        # 处理 request_data，将非标准格式的 'image_url' 转换为标准格式
        for message in request_data.get('messages', []):
            if 'content' in message and isinstance(message['content'], list):
                urls = []
                texts = []
                for item in message['content']:
                    if item.get('type') == 'text':
                        texts.append(item.get('text', ''))
                    elif item.get('type') == 'image_url':
                        image_url_field = item.get('image_url', '')
                        if isinstance(image_url_field, dict):
                            image_url = image_url_field.get('url', '')
                        elif isinstance(image_url_field, str):
                            image_url = image_url_field
                        else:
                            logger.warning(f"Unrecognized image_url format: {image_url_field}")
                            continue
                        if image_url:
                            urls.append(image_url)
                # 拼接 URLs 和 texts，并用 '\n ' 分隔
                new_content = "\n ".join(urls + texts).strip()
                # 将新内容赋值回 message['content']
                message['content'] = new_content

        # 输出提取赋值后的 request_data 内容
        logger.debug(f"Extracted request data: {request_data}")
    except Exception as e:
        logger.error(f"Error processing request data: {e}")
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})

    chat_service, res = await async_retry(process, request_data, req_token)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(chat_service.close_client)
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background)
    except HTTPException as e:
        await chat_service.close_client()
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")
    
@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
            with open("data/token.txt", "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def upload_post():
    globals.token_list.clear()
    globals.error_token_list.clear()
    with open("data/token.txt", "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/tokens/add/{{token}}" if api_prefix else "/tokens/add/{token}")
async def add_token(token: str):
    if token.strip() and not token.startswith("#"):
        globals.token_list.append(token.strip())
        with open("data/token.txt", "a", encoding="utf-8") as f:
            f.write(token.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


if enable_gateway:
    @app.get("/", response_class=HTMLResponse)
    async def chatgpt_html(request: Request):
        token = request.query_params.get("token")
        if not token:
            token = request.cookies.get("token")
        if not token:
            return await login_html(request)

        response = templates.TemplateResponse("chatgpt.html", {"request": request, "token": token})
        response.set_cookie("token", value=token)
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_html(request: Request):
        response = templates.TemplateResponse("login.html", {"request": request})
        return response


    @app.get("/backend-api/gizmos/bootstrap")
    async def get_gizmos_bootstrap():
        return {"gizmos": []}


    # @app.get("/backend-api/conversations")
    # async def get_conversations():
    #     return {"items": [], "total": 0, "limit": 28, "offset": 0, "has_missing_conversations": False}

    # @app.patch("/backend-api/conversations")
    # async def get_conversations():
    #     return {"success": True, "message": None}


    @app.get("/backend-api/me")
    async def get_me(request: Request):
        me = {
            "object": "user",
            "id": "org-chatgpt",
            "email": "chatgpt@openai.com",
            "name": "ChatGPT",
            "picture": "https://cdn.auth0.com/avatars/ai.png",
            "created": int(time.time()),
            "phone_number": None,
            "mfa_flag_enabled": False,
            "amr": [],
            "groups": [],
            "orgs": {
                "object": "list",
                "data": [
                    {
                        "object": "organization",
                        "id": "org-chatgpt",
                        "created": 1715641300,
                        "title": "Personal",
                        "name": "user-chatgpt",
                        "description": "Personal org for chatgpt@openai.com",
                        "personal": True,
                        "settings": {},
                        "parent_org_id": None,
                        "is_default": False,
                        "role": "owner",
                        "is_scale_tier_authorized_purchaser": None,
                        "is_scim_managed": False,
                        "projects": {
                            "object": "list",
                            "data": []
                        },
                        "groups": [],
                        "geography": None
                    }
                ]
            },
            "has_payg_project_spend_limit": None
        }
        return me


    banned_paths = [
        "backend-api/accounts/logout_all",
        "backend-api/accounts/deactivate",
        "backend-api/user_system_messages",
        "backend-api/memories",
        "backend-api/settings/clear_account_user_memory"
    ]
    redirect_paths = ["auth/logout"]
    chatgpt_paths = ["c/"]


@app.get("/")
async def chatgpt(request: Request):
    if not enable_gateway:
        raise HTTPException(status_code=404, detail="Gateway is disabled")
    req_token = get_req_token(authorization_list[0])
    access_token = await verify_token(req_token)
    response = templates.TemplateResponse("chatgpt.html", {"request": request, "access_token": access_token})
    response.set_cookie("req_token", value=req_token)
    response.set_cookie("access_token", value=access_token)
    return response


    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
    async def reverse_proxy(request: Request, path: str):
        for chatgpt_path in chatgpt_paths:
            if chatgpt_path in path:
                return await chatgpt_html(request)

        for banned_path in banned_paths:
            if banned_path in path:
                return Response(status_code=404)

        for redirect_path in redirect_paths:
            if redirect_path in path:
                redirect_url = str(request.base_url)
                response = RedirectResponse(url=f"{redirect_url}", status_code=302)
                response.delete_cookie("token")
                return response

        return await chatgpt_reverse_proxy(request, path)
else:
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
    async def reverse_proxy():
        raise HTTPException(status_code=404, detail="Gateway is disabled")
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"])
async def reverse_proxy(request: Request, path: str):
    if path.startswith("c/"):
        redirect_url = str(request.base_url)
        return RedirectResponse(url=redirect_url, status_code=302)
    return await chatgpt_reverse_proxy(request, path)
