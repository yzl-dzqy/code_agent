---
name: curl-api-testing
description: 使用 curl 进行 API 测试的指引
---

# 使用 curl 进行 API 测试

## 概述

`curl` 是一个强大的命令行工具，用于发送 HTTP 请求，常用于测试 RESTful API。

## 基本用法

### GET 请求

```bash
curl <URL>
```

#### 示例
<good-example>
```bash
curl https://api.example.com/users
```
</good-example>

### POST 请求

使用 `-X POST` 指定方法，`-H "Content-Type: application/json"` 指定请求头，`-d '{"key": "value"}'` 指定请求体。

```bash
curl -X POST -H "Content-Type: application/json" -d '{"key": "value"}' <URL>
```

#### 示例
<good-example>
```bash
curl -X POST -H "Content-Type: application/json" -d '{"name": "John Doe", "email": "john.doe@example.com"}' https://api.example.com/users
```
</good-example>

### PUT 请求

与 POST 类似，使用 `-X PUT` 指定方法。

```bash
curl -X PUT -H "Content-Type: application/json" -d '{"key": "new_value"}' <URL>
```

#### 示例
<good-example>
```bash
curl -X PUT -H "Content-Type: application/json" -d '{"name": "Jane Doe"}' https://api.example.com/users/123
```
</good-example>

### DELETE 请求

使用 `-X DELETE` 指定方法。

```bash
curl -X DELETE <URL>
```

#### 示例
<good-example>
```bash
curl -X DELETE https://api.example.com/users/123
```
</good-example>

## 高级用法

### 添加请求头

使用 `-H` 参数添加自定义请求头。

```bash
curl -H "Authorization: Bearer <TOKEN>" <URL>
```

#### 示例
<good-example>
```bash
curl -H "Authorization: Bearer my_secret_token" https://api.example.com/protected_resource
```
</good-example>

### 基本认证

使用 `-u` 参数提供用户名和密码。

```bash
curl -u "username:password" <URL>
```

#### 示例
<good-example>
```bash
curl -u "admin:password123" https://api.example.com/admin_resource
```
</good-example>

### 保存响应到文件

使用 `-o` 参数将响应保存到文件。

```bash
curl <URL> -o output.json
```

#### 示例
<good-example>
```bash
curl https://api.example.com/data -o data.json
```
</good-example>

### 显示请求和响应头

使用 `-v` 参数显示详细的请求和响应信息。

```bash
curl -v <URL>
```

#### 示例
<good-example>
```bash
curl -v https://api.example.com/status
```
</good-example>

### 发送表单数据

对于 `application/x-www-form-urlencoded` 类型的表单数据，使用 `-d` 参数。

```bash
curl -X POST -d "param1=value1&param2=value2" <URL>
```

#### 示例
<good-example>
```bash
curl -X POST -d "username=testuser&password=testpass" https://api.example.com/login
```
</good-example>

### 上传文件

使用 `-F` 参数上传文件。

```bash
curl -X POST -F "file=@/path/to/your/file.txt" <URL>
```

#### 示例
<good-example>
```bash
curl -X POST -F "image=@/home/user/picture.jpg" https://api.example.com/upload
```
</good-example>
