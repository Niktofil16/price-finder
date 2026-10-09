import React, { useState, useEffect } from 'react';
import { Copy, Check, Download, Terminal, FileCode2 } from 'lucide-react';

export default function App() {
  const [pythonCode, setPythonCode] = useState<string>('');
  const [requirementsCode, setRequirementsCode] = useState<string>('');
  const [copiedPy, setCopiedPy] = useState(false);
  const [copiedReq, setCopiedReq] = useState(false);

  useEffect(() => {
    fetch('/api/python-code')
      .then((r) => r.json())
      .then((data) => {
        setPythonCode(data.code || '');
        setRequirementsCode(data.requirements || '');
      })
      .catch(() => {});
  }, []);

  const handleCopyPython = () => {
    if (!pythonCode) return;
    navigator.clipboard.writeText(pythonCode);
    setCopiedPy(true);
    setTimeout(() => setCopiedPy(false), 2000);
  };

  const handleCopyReq = () => {
    if (!requirementsCode) return;
    navigator.clipboard.writeText(requirementsCode);
    setCopiedReq(true);
    setTimeout(() => setCopiedReq(false), 2000);
  };

  const handleDownloadPython = () => {
    if (!pythonCode) return;
    const blob = new Blob([pythonCode], { type: 'text/x-python;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'main.py';
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-6 md:p-10">
      <div className="max-w-5xl mx-auto space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-800 pb-6">
          <div className="space-y-1">
            <h1 className="text-xl font-bold tracking-tight text-white">
              BozorScan AI — Чистый Telegram-бот (без Web-версии)
            </h1>
            <p className="text-xs text-slate-400">
              Полностью удалены FastAPI / Uvicorn / Web-роуты. Добавлено автоматическое завершение старых дублирующих процессов и динамический выбор модели через ListModels.
            </p>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={handleCopyPython}
              className="inline-flex items-center gap-2 px-4 py-2 text-xs font-semibold text-slate-900 bg-white hover:bg-slate-200 rounded-lg transition-colors whitespace-nowrap shrink-0 cursor-pointer"
            >
              {copiedPy ? (
                <>
                  <Check className="w-4 h-4 text-emerald-600" />
                  Скопировано
                </>
              ) : (
                <>
                  <Copy className="w-4 h-4" />
                  Скопировать main.py
                </>
              )}
            </button>

            <button
              onClick={handleDownloadPython}
              className="inline-flex items-center gap-2 px-4 py-2 text-xs font-semibold text-white bg-blue-600 hover:bg-blue-500 rounded-lg transition-colors whitespace-nowrap shrink-0 cursor-pointer"
            >
              <Download className="w-4 h-4" />
              Скачать main.py
            </button>
          </div>
        </div>

        {/* Quick Run Instructions in VS Code */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="p-4 bg-slate-900 border border-slate-800 rounded-xl space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
                <Terminal className="w-3.5 h-3.5 text-blue-400" />
                Запуск в терминале VS Code
              </span>
            </div>
            <pre className="font-mono text-xs text-emerald-400 bg-slate-950 p-3 rounded-lg overflow-x-auto">
{`pip install -r requirements.txt
python main.py`}
            </pre>
            <p className="text-xs text-slate-400">
              При старте <code className="font-mono text-slate-300">main.py</code> автоматически останавливает все старые зависшие процессы бота и блокирует повторный запуск через системный сокет.
            </p>
          </div>

          <div className="p-4 bg-slate-900 border border-slate-800 rounded-xl space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-300 flex items-center gap-1.5">
                <FileCode2 className="w-3.5 h-3.5 text-blue-400" />
                requirements.txt (без FastAPI и Uvicorn)
              </span>
              <button
                onClick={handleCopyReq}
                className="text-xs text-slate-400 hover:text-white flex items-center gap-1 cursor-pointer"
              >
                {copiedReq ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                {copiedReq ? 'Скопировано' : 'Копировать'}
              </button>
            </div>
            <pre className="font-mono text-xs text-slate-200 bg-slate-950 p-3 rounded-lg overflow-x-auto">
              {requirementsCode}
            </pre>
          </div>
        </div>

        {/* Full main.py Source Code */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-800 flex items-center justify-between">
            <span className="font-mono text-xs font-semibold text-slate-300">main.py</span>
            <span className="text-xs text-slate-500">Только Telegram-бот · ListModels · Защита от дублей</span>
          </div>
          <div className="p-4 bg-slate-950 overflow-x-auto max-h-[650px]">
            <pre className="font-mono text-xs text-slate-200 leading-relaxed">{pythonCode}</pre>
          </div>
        </div>
      </div>
    </div>
  );
}
