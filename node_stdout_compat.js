// 旧版资源脚本会直接调用 TTY 方法；服务端将输出重定向到日志时补充无副作用的兼容实现。
for (const method of ["clearLine", "cursorTo", "moveCursor"]) {
  if (typeof process.stdout[method] !== "function") {
    process.stdout[method] = () => false;
  }
}
