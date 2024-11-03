import asyncio
import json
import random
import re
import string
import time
import uuid

import pybase64
import websockets
from fastapi import HTTPException

from api.files import get_file_content
from api.models import model_system_fingerprint
from api.tokens import split_tokens_from_content, calculate_image_tokens, num_tokens_from_messages
from utils.Logger import logger
import urllib.parse
import os
from utils.config import file_proxy_url

###R2客户端
import boto3
import aiohttp

# 从环境变量中读取 Cloudflare R2 配置
R2_ACCESS_KEY_ID = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("CLOUDFLARE_R2_BUCKET_NAME")
R2_ACCOUNT_ID = os.getenv("CLOUDFLARE_R2_ACCOUNT_ID")
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN")  

# 配置 R2 客户端
s3_client = boto3.client(
    's3',
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY
)

async def upload_to_r2(local_image_path, object_name):
    try:
        # 上传文件到 Cloudflare R2
        s3_client.upload_file(local_image_path, R2_BUCKET_NAME, object_name)
        
        # 生成公开的 URL
        r2_url = f"{PUBLIC_DOMAIN}/{object_name}"
        return r2_url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload file to R2: {str(e)}")



def generate_download_link(file_download_url, index):
    if file_proxy_url:
         # 解析原始 URL
        parsed_url = urllib.parse.urlparse(file_download_url)
        file_path = parsed_url.path
        query = parsed_url.query
        fragment = parsed_url.fragment
        
        # 拼接路径部分
        proxy_download_url = urllib.parse.urljoin(file_proxy_url, file_path.lstrip('/'))
        # 重新组合完整的 URL
        if query:
            proxy_download_url = f"{proxy_download_url}?{query}"
        if fragment:
            proxy_download_url = f"{proxy_download_url}#{fragment}"

        return f"\n[请点击这里下载，不要点击上面 {index+1}]({proxy_download_url})\n"
    else:
        return f"\n[请点击这里下载，不要点击上面 {index+1}]({file_download_url})\n"
   

moderation_message = "I'm sorry, I cannot provide or engage in any content related to pornography, violence, or any unethical material. If you have any other questions or need assistance, please feel free to let me know. I'll do my best to provide support and assistance."


async def format_not_stream_response(response, prompt_tokens, max_tokens, model):
    chat_id = f"chatcmpl-{''.join(random.choice(string.ascii_letters + string.digits) for _ in range(29))}"
    system_fingerprint_list = model_system_fingerprint.get(model, None)
    system_fingerprint = random.choice(system_fingerprint_list) if system_fingerprint_list else None
    created_time = int(time.time())
    all_text = ""
    async for chunk in response:
        try:
            if chunk.startswith("data: [DONE]"):
                break
            elif not chunk.startswith("data: "):
                continue
            else:
                chunk = json.loads(chunk[6:])
                if not chunk["choices"][0].get("delta"):
                    continue
                all_text += chunk["choices"][0]["delta"]["content"]
        except Exception as e:
            logger.error(f"Error: {chunk}, error: {str(e)}")
            continue
    content, completion_tokens, finish_reason = await split_tokens_from_content(all_text, max_tokens, model)
    message = {
        "role": "assistant",
        "content": content,
    }
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens
    }
    if not message.get("content"):
        raise HTTPException(status_code=403, detail="No content in the message.")

    data = {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_time,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish_reason
            }
        ],
        "usage": usage
    }
    if system_fingerprint:
        data["system_fingerprint"] = system_fingerprint
    return data


async def wss_stream_response(websocket, conversation_id):
    while not websocket.closed:
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=10)
            if message:
                resultObj = json.loads(message)
                sequenceId = resultObj.get("sequenceId", None)
                if not sequenceId:
                    continue
                data = resultObj.get("data", {})
                if conversation_id != data.get("conversation_id", ""):
                    continue
                sequenceId = resultObj.get('sequenceId')
                if sequenceId and sequenceId % 80 == 0:
                    await websocket.send(
                        json.dumps(
                            {"type": "sequenceAck", "sequenceId": sequenceId}
                        )
                    )
                decoded_bytes = pybase64.b64decode(data.get("body", None))
                yield decoded_bytes
            else:
                print("No message received within the specified time.")
        except asyncio.TimeoutError:
            logger.error("Timeout! No message received within the specified time.")
            break
        except websockets.ConnectionClosed as e:
            if e.code == 1000:
                logger.error("WebSocket closed normally with code 1000 (OK)")
                yield b"data: [DONE]\n\n"
            else:
                logger.error(f"WebSocket closed with error code {e.code}")
        except Exception as e:
            logger.error(f"Error: {str(e)}")
            continue


async def head_process_response(response):
    async for chunk in response:
        chunk = chunk.decode("utf-8")
        if chunk.startswith("data: {"):
            chunk_old_data = json.loads(chunk[6:])
            message = chunk_old_data.get("message", {})
            if not message and "error" in chunk_old_data:
                return response, False
            role = message.get('author', {}).get('role')
            if role == 'user' or role == 'system':
                continue

            status = message.get("status")
            if status == "in_progress":
                return response, True
    return response, False


async def stream_response(service, response, model, max_tokens):
    chat_id = f"chatcmpl-{''.join(random.choice(string.ascii_letters + string.digits) for _ in range(29))}"
    system_fingerprint_list = model_system_fingerprint.get(model, None)
    system_fingerprint = random.choice(system_fingerprint_list) if system_fingerprint_list else None
    created_time = int(time.time())
    completion_tokens = 0
    len_last_content = 0
    len_last_citation = 0
    last_message_id = None
    last_role = None
    last_content_type = None
    model_slug = None
    end = False

    chunk_new_data = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created_time,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "logprobs": None,
                "finish_reason": None
            }
        ]
    }
    if system_fingerprint:
        chunk_new_data["system_fingerprint"] = system_fingerprint
    yield f"data: {json.dumps(chunk_new_data)}\n\n"

    async for chunk in response:
        chunk = chunk.decode("utf-8")
        if end:
            logger.info(f"Response Model: {model_slug}")
            yield "data: [DONE]\n\n"
            break
        try:
            if chunk.startswith("data: {"):
                chunk_old_data = json.loads(chunk[6:])
                #这里是20240621但是单独为了修复文件浏览问题而设置的，但是目前貌似没有需要了
                # recipient = chunk_old_data.get("message", {}).get("recipient", "")
                # # 跳过特定响应
                # if recipient == "myfiles_browser":
                #     continue
                
                finish_reason = None
                message = chunk_old_data.get("message", {})
                conversation_id = chunk_old_data.get("conversation_id")
                role = message.get('author', {}).get('role')
                if role == 'user' or role == 'system':
                    continue

                status = message.get("status")
                message_id = message.get("id")
                content = message.get("content", {})
                recipient = message.get("recipient", "")
                meta_data = message.get("metadata", {})
                initial_text = meta_data.get("initial_text", "")
                model_slug = meta_data.get("model_slug", model_slug)

                if not message and chunk_old_data.get("type") == "moderation":
                    delta = {"role": "assistant", "content": moderation_message}
                    finish_reason = "stop"
                    end = True
                elif status == "in_progress":
                    outer_content_type = content.get("content_type")
                    if outer_content_type == "text":
                        part = content.get("parts", [])[0]
                        if not part:
                            if role == 'assistant' and last_role != 'assistant':
                                if last_role == None:
                                    new_text = ""
                                else:
                                    new_text = f"\n"
                            elif role == 'tool' and last_role != 'tool':
                                new_text = f">{initial_text}\n"
                            else:
                                new_text = ""
                        else:
                            if last_message_id and last_message_id != message_id:
                                continue
                            citation = message.get("metadata", {}).get("citations", [])
                            if len(citation) > len_last_citation:
                                inside_metadata = citation[-1].get("metadata", {})
                                citation_title = inside_metadata.get("title", "")
                                citation_url = inside_metadata.get("url", "")
                                new_text = f' **[[""]]({citation_url} "{citation_title}")** '
                                len_last_citation = len(citation)
                            else:
                                if role == 'assistant' and last_role != 'assistant':
                                    if recipient == 'dalle.text2im':
                                        # new_text = f"\n```{recipient}\n{part[len_last_content:]}"
                                        new_text = f"\n\n{part[len_last_content:]}"
                                    elif last_role == None:
                                        new_text = part[len_last_content:]
                                    else:
                                        new_text = f"\n\n{part[len_last_content:]}"
                                elif role == 'tool' and last_role != 'tool':
                                    new_text = f">{initial_text}\n{part[len_last_content:]}"
                                elif role == 'tool':
                                    new_text = part[len_last_content:].replace("\n\n", "\n")
                                else:
                                    new_text = part[len_last_content:]
                            len_last_content = len(part)
                    else:
                        text = content.get("text", "")
                        if outer_content_type == "code" and last_content_type != "code":
                            language = content.get("language", "")
                            if not language or language == "unknown":
                                language = recipient
                            new_text = "\n```" + language + "\n" + text[len_last_content:]
                        elif outer_content_type == "execution_output" and last_content_type != "execution_output":
                            new_text = "\n```" + "Output" + "\n" + text[len_last_content:]
                        else:
                            new_text = text[len_last_content:]
                        len_last_content = len(text)
                    if last_content_type == "code" and outer_content_type != "code":
                        new_text = "\n```\n" + new_text
                    elif last_content_type == "execution_output" and outer_content_type != "execution_output":
                        new_text = "\n```\n" + new_text

                    delta = {"content": new_text}
                    last_content_type = outer_content_type
                    if completion_tokens >= max_tokens:
                        delta = {}
                        finish_reason = "length"
                        end = True
                elif status == "finished_successfully":
                    if content.get("content_type") == "multimodal_text":
                        parts = content.get("parts", [])
                        delta = {}
                        for part in parts:
                            if isinstance(part, str):
                                continue
                            inner_content_type = part.get('content_type')
                            if inner_content_type == "image_asset_pointer":
                                last_content_type = "image_asset_pointer"
                                file_id = part.get('asset_pointer').replace('file-service://', '')
                                logger.debug(f"file_id: {file_id}")
                                
                                # 获取本地文件的临时下载 URL
                                image_download_url = await service.get_download_url(file_id)
                                logger.debug(f"image_download_url: {image_download_url}")
                                
                                if image_download_url:
                                    # 下载并保存本地临时文件
                                    local_image_path = f"/tmp/{file_id}.webp"
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(image_download_url) as resp:
                                            if resp.status == 200:
                                                with open(local_image_path, 'wb') as f:
                                                    f.write(await resp.read())
                                            else:
                                                raise HTTPException(status_code=404, detail="Failed to download image")
                                    
                                    # 上传图片到 R2
                                    r2_image_url = await upload_to_r2(local_image_path, f"{file_id}.webp")
                                    logger.debug(f"R2 Image URL: {r2_image_url}")
                                    
                                    # 删除本地临时文件
                                    os.remove(local_image_path)

                                    # 使用 R2 URL 生成输出内容
                                    delta = {"content": f"\n```\n\n![image]({r2_image_url})\n\n"}
                                else:
                                    delta = {"content": f"\n```\n\nFailed to load the image.\n"}
                    elif message.get("end_turn"):
                        part = content.get("parts", [])[0]
                        new_text = part[len_last_content:]
                        if not new_text:
                            matches = re.findall(r'\(sandbox:(.*?)\)', part)
                            if matches:
                                file_url_content = ""
                                for i, sandbox_path in enumerate(matches):
                                    file_download_url = await service.get_response_file_url(conversation_id, message_id, sandbox_path)
                                    if file_download_url:
                                        # file_url_content += f"\n```\n\n![File {i+1}]({file_download_url})\n"
                                        file_url_content += generate_download_link(file_download_url, i)
                                delta = {"content": file_url_content}
                            else:
                                delta = {}
                        else:
                            delta = {"content": new_text}
                        finish_reason = "stop"
                        end = True
                    else:
                        len_last_content = 0
                        if meta_data.get("finished_text"):
                            delta = {"content": f"\n{meta_data.get('finished_text')}\n"}
                        else:
                            continue
                else:
                    continue
                last_message_id = message_id
                last_role = role
                if not end and not delta.get("content"):
                    delta = {"role": "assistant", "content": ""}
                chunk_new_data["choices"][0]["delta"] = delta
                chunk_new_data["choices"][0]["finish_reason"] = finish_reason
                if not service.history_disabled:
                    chunk_new_data.update({
                        "message_id": message_id,
                        "conversation_id": conversation_id,
                    })
                completion_tokens += 1
                yield f"data: {json.dumps(chunk_new_data)}\n\n"
            elif chunk.startswith("data: [DONE]"):
                logger.info(f"Response Model: {model_slug}")
                yield "data: [DONE]\n\n"
            else:
                continue
        except Exception as e:
            if chunk.startswith("data: "):
                chunk_data = json.loads(chunk[6:])
                if chunk_data.get("error"):
                    logger.error(f"Error: {chunk_data.get('error')}")
                    yield "data: [DONE]\n\n"
                    break
            logger.error(f"Error: {chunk}, details: {str(e)}")
            continue


def get_url_from_content(content):
    if isinstance(content, str) and content.startswith('http'):
        try:
            url = re.match(
                r'(?i)\b((?:[a-z][\w-]+:(?:/{1,3}|[a-z0-9%])|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?«»“”‘’]))',
                content.split(' ')[0])[0]
            content = content.replace(url, '').strip()
            return url, content
        except Exception:
            return None, content
    return None, content


def format_messages_with_url(content):
    url_list = []
    while True:
        url, content = get_url_from_content(content)
        if url:
            url_list.append(url)
            logger.info(f"Found a file_url from messages: {url}")
        else:
            break
    if not url_list:
        return content
    new_content = [
        {
            "type": "text",
            "text": content
        }
    ]
    for url in url_list:
        new_content.append({
            "type": "image_url",
            "image_url": {
                "url": url
            }
        })
    return new_content


async def api_messages_to_chat(service, api_messages, upload_by_url=False):
    file_tokens = 0
    chat_messages = []
    for api_message in api_messages:
        role = api_message.get('role')
        content = api_message.get('content')
        if upload_by_url:
            if isinstance(content, str):
                content = format_messages_with_url(content)
        if isinstance(content, list):
            parts = []
            attachments = []
            content_type = "multimodal_text"
            for i in content:
                if i.get("type") == "text":
                    parts.append(i.get("text"))
                elif i.get("type") == "image_url":
                    image_url = i.get("image_url")
                    url = image_url.get("url")
                    detail = image_url.get("detail", "auto")
                    file_content, mime_type = await get_file_content(url)
                    file_meta = await service.upload_file(file_content, mime_type)
                    if file_meta:
                        file_id = file_meta["file_id"]
                        file_size = file_meta["size_bytes"]
                        file_name = file_meta["file_name"]
                        mime_type = file_meta["mime_type"]
                        use_case = file_meta["use_case"]
                        if mime_type.startswith("image/"):
                            width, height = file_meta["width"], file_meta["height"]
                            file_tokens += await calculate_image_tokens(width, height, detail)
                            parts.append({
                                # "content_type": "image_asset_pointer",
                                #Oaifree代理貌似不能用这个，但是现在不使用oaifree代理，所以恢复原本正常
                                "content_type": "image_asset_pointer",
                                "asset_pointer": f"file-service://{file_id}",
                                "size_bytes": file_size,
                                "width": width,
                                "height": height
                            })
                            attachments.append({
                                "id": file_id,
                                "size": file_size,
                                "name": file_name,
                                "mime_type": mime_type,
                                "width": width,
                                "height": height
                            })
                        else:
                            if not use_case == "ace_upload":
                                await service.check_upload(file_id)
                            file_tokens += file_size // 1000
                            attachments.append({
                                "id": file_id,
                                "size": file_size,
                                "name": file_name,
                                "mime_type": mime_type,
                            })
                    else:
                        # 上传失败，处理方式
                        logger.error(f"Failed to upload file from URL: {url}")
                        # 选项1：跳过此文件
                        continue
                        # 选项2：抛出异常
                        # raise Exception(f"Failed to upload file from URL: {url}")
            metadata = {
                "attachments": attachments
            }
        else:
            content_type = "text"
            parts = [content]
            metadata = {}
        chat_message = {
            "id": f"{uuid.uuid4()}",
            "author": {"role": role},
            "content": {"content_type": content_type, "parts": parts},
            "metadata": metadata
        }
        chat_messages.append(chat_message)
    text_tokens = await num_tokens_from_messages(api_messages, service.resp_model)
    prompt_tokens = text_tokens + file_tokens
    return chat_messages, prompt_tokens
