<template>
  <div ref="editorContainer" class="monaco-editor-container"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue';
import * as monaco from 'monaco-editor';

// 简化 Monaco 配置，不使用 worker 避免构建问题
self.MonacoEnvironment = {
  getWorker: function () {
    return {
      addEventListener: () => {},
      removeEventListener: () => {},
      postMessage: () => {},
      terminate: () => {}
    } as any;
  }
};

interface Props {
  modelValue: string;
  language?: string;
  theme?: string;
  options?: monaco.editor.IStandaloneEditorConstructionOptions;
}

interface Emits {
  (e: 'update:modelValue', value: string): void;
  (e: 'change', value: string): void;
  (e: 'save'): void;           // Ctrl+S 触发
  (e: 'run'): void;            // Ctrl+Enter 触发
  (e: 'format'): void;         // Ctrl+Shift+F 触发
}

const props = withDefaults(defineProps<Props>(), {
  language: 'python',
  theme: 'vs-dark',
  options: () => ({}),
});

const emit = defineEmits<Emits>();

const editorContainer = ref<HTMLElement>();
let editor: monaco.editor.IStandaloneCodeEditor | null = null;

// Python 语法高亮配置
const pythonLanguageConfig = {
  comments: {
    lineComment: '#',
    blockComment: ['"""', '"""'],
  },
  brackets: [
    ['{', '}'],
    ['[', ']'],
    ['(', ')'],
  ],
  autoClosingPairs: [
    { open: '{', close: '}' },
    { open: '[', close: ']' },
    { open: '(', close: ')' },
    { open: '"', close: '"' },
    { open: "'", close: "'" },
  ],
  surroundingPairs: [
    { open: '{', close: '}' },
    { open: '[', close: ']' },
    { open: '(', close: ')' },
    { open: '"', close: '"' },
    { open: "'", close: "'" },
  ],
};

// 策略框架代码补全提示
const strategySnippets = [
  {
    label: 'strategy_template',
    kind: monaco.languages.CompletionItemKind.Snippet,
    insertText: `from core.quant.strategies.strategy_framework import StrategyBase

class MyStrategy(StrategyBase):
    """
    策略描述
    """
    strategy_name = "我的策略"
    
    def __init__(self, name=None):
        super().__init__(name)
    
    def generate_signals(self, current_date, stock_pool_today, data_query):
        """
        策略逻辑
        """
        signals = {}
        
        for _, row in stock_pool_today.iterrows():
            code = row['stock_code']
            
            # 买入条件
            if (row['close'] > row['ma20'] and 
                row['volume_ratio'] > 2.0 and
                not row['is_st']):
                signals[code] = 'buy'
            
            # 卖出条件
            elif row['close'] < row['ma5']:
                signals[code] = 'sell'
            
            else:
                signals[code] = 'hold'
        
        return signals`,
    detail: '策略基础模板',
    documentation: '创建一个基础策略类模板',
  },
  {
    label: 'buy_condition',
    kind: monaco.languages.CompletionItemKind.Snippet,
    insertText: `if (row['close'] > row['ma20'] and 
    row['volume_ratio'] > 2.0 and
    not row['is_st'] and
    not row['is_limit_up']):
    signals[code] = 'buy'`,
    detail: '买入条件模板',
  },
  {
    label: 'sell_condition',
    kind: monaco.languages.CompletionItemKind.Snippet,
    insertText: `elif row['close'] < row['ma5']:
    signals[code] = 'sell'`,
    detail: '卖出条件模板',
  },
  {
    label: 'rich_signal',
    kind: monaco.languages.CompletionItemKind.Snippet,
    insertText: `signals[code] = {
    'action': 'buy',
    'weight': 0.2,
    'score': 1.5,
    'params': {
        'reason': '突破均线'
    }
}`,
    detail: '富信号格式',
  },
];

// 注册 Python 代码补全
const registerPythonCompletion = () => {
  monaco.languages.registerCompletionItemProvider('python', {
    provideCompletionItems: (model, position) => {
      const word = model.getWordUntilPosition(position);
      const range = {
        startLineNumber: position.lineNumber,
        endLineNumber: position.lineNumber,
        startColumn: word.startColumn,
        endColumn: word.endColumn,
      };

      return {
        suggestions: strategySnippets.map((snippet) => ({
          ...snippet,
          range,
        })),
      };
    },
  });
};

// 初始化编辑器
const initEditor = () => {
  if (!editorContainer.value) return;

  // 设置 Python 语言配置
  monaco.languages.setLanguageConfiguration('python', pythonLanguageConfig);
  
  // 注册代码补全
  registerPythonCompletion();

  editor = monaco.editor.create(editorContainer.value, {
    value: props.modelValue,
    language: props.language,
    theme: props.theme,
    automaticLayout: true,
    minimap: {
      enabled: true,
      scale: 1,
    },
    fontSize: 14,
    fontFamily: 'Consolas, "Courier New", monospace',
    lineNumbers: 'on',
    roundedSelection: false,
    scrollBeyondLastLine: false,
    readOnly: false,
    cursorStyle: 'line',
    tabSize: 4,
    insertSpaces: true,
    wordWrap: 'on',
    folding: true,
    foldingStrategy: 'indentation',
    showFoldingControls: 'always',
    matchBrackets: 'always',
    autoIndent: 'full',
    formatOnPaste: true,
    formatOnType: true,
    ...props.options,
  });

  // 监听内容变化
  editor.onDidChangeModelContent(() => {
    const value = editor?.getValue() || '';
    emit('update:modelValue', value);
    emit('change', value);
  });

  // 注册 VS Code 风格快捷键
  // Ctrl+S：保存策略（阻止浏览器默认保存对话框）
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
    emit('save');
  });

  // Ctrl+Enter：运行回测
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
    emit('run');
  });

  // Ctrl+Shift+F：格式化代码
  editor.addCommand(
    monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyF,
    () => {
      editor?.getAction('editor.action.formatDocument')?.run();
      emit('format');
    }
  );

  // F1：聚焦命令面板（保留 Monaco 默认 F1 命令面板，VS Code 风格）
};

// 监听外部值变化
watch(
  () => props.modelValue,
  (newValue) => {
    if (editor && editor.getValue() !== newValue) {
      editor.setValue(newValue);
    }
  }
);

// 监听主题变化
watch(
  () => props.theme,
  (newTheme) => {
    if (editor) {
      monaco.editor.setTheme(newTheme);
    }
  }
);

// 获取编辑器实例
const getEditor = () => editor;

// 插入代码
const insertCode = (code: string) => {
  if (editor) {
    const position = editor.getPosition();
    if (position) {
      editor.executeEdits('', [
        {
          range: new monaco.Range(
            position.lineNumber,
            position.column,
            position.lineNumber,
            position.column
          ),
          text: code,
        },
      ]);
    }
  }
};

// 格式化代码
const formatCode = () => {
  if (editor) {
    editor.getAction('editor.action.formatDocument')?.run();
  }
};

defineExpose({
  getEditor,
  insertCode,
  formatCode,
});

onMounted(() => {
  initEditor();
});

onBeforeUnmount(() => {
  if (editor) {
    editor.dispose();
    editor = null;
  }
});
</script>

<style scoped>
.monaco-editor-container {
  width: 100%;
  height: 100%;
  min-height: 400px;
}
</style>
