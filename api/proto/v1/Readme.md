# The Contract

In a distributed system like this (Go + Python + NATS), you cannot just "send JSON strings" and hope the other side understands them. That leads to crashes when a Python worker expects `user_id` (string) but the Go server sends `userID` (int).

_We use Protocol Buffers (Protobuf). This is a language-neutral way to define your data._

1. You write `.proto` files once.

2. You run a compiler (`protoc`).

3. It generates Go code for your API.

4. It generates Python code for your Worker.

If you change a field in the proto file, the compiler updates the code in both languages automatically. This guarantees that your Backend and Worker never disagree.

### Here are the Two Critical Contracts for the e2e Platform.

1. **The API Contract (`workflow.proto`)**
   This defines how the Frontend (Next.js) talks to the Backend (Go) via gRPC.

2. **The Event Contract (`events.proto`)**
   This defines the messages flying through your "Nervous System" (NATS). These are asynchronous events.

# Explanation

1. `syntax = "proto3"`: We are using the latest version of Protobuf.

2. `package v1`: This namespaces your code so that if you make a v2 later, it won't break existing code.

3. `service WorkflowService`: This automatically generates a Go Interface and a Python Abstract Class. You just have to "fill in the blanks" in your code. You don't need to write boilerplate HTTP routers.

4. **Numbers (`= 1, = 2`): These are Field Tags.**
   - In JSON, we send {`"user_id": "123"`} (key is a string, waste of space).
   - In Protobuf, we send `0x01 <value>`. The computer knows 0x01 means user_id. This is why it is 10x smaller and faster.

## How to run:

1. **Compile for `GO`**

```bash
protoc --proto_path=api/proto/v1 \
       --go_out=api/gen/go/v1 --go_opt=paths=source_relative \
       --go-grpc_out=api/gen/go/v1 --go-grpc_opt=paths=source_relative \
       api/proto/v1/*.proto

```

1. **Compile for `PYTHON`**

```bash
python -m grpc_tools.protoc -Iapi/proto/v1 \
       --python_out=api/gen/python/v1 \
       --grpc_python_out=api/gen/python/v1 \
       api/proto/v1/*.proto
```

### Before this install following

## 1. Install the Go protobuf plugins

```bash
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
```

For python

```python
python3 -m pip install --upgrade pip
python3 -m pip install grpcio-tools
```

Now confirm they were installed:

```bash
ls $(go env GOPATH)/bin
```

## 2. Add GOPATH/bin to PATH

```bash
echo 'export PATH="$PATH:$(go env GOPATH)/bin"' >> ~/.zshrc
source ~/.zshrc
```
