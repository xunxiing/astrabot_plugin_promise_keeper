import torch
from transformers import BertTokenizer, BertForSequenceClassification
import os

class PromiseDetector:
    """
    一个用于检测中文承诺的预测器。
    这个类被设计为在应用启动时初始化一次，然后可以被重复调用进行预测。
    """
    def __init__(self, model_path=None):
        """
        初始化模型和分词器。

        Args:
            model_path (str, optional): 模型文件所在的目录路径。
                                        如果为 None，则默认使用同级目录下的 'models' 文件夹。
        """
        if model_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            model_path = os.path.join(base_dir, 'models')

        # --- 核心修正：同时检查 .bin 和 .safetensors 文件 ---
        bin_file = os.path.join(model_path, 'pytorch_model.bin')
        safetensors_file = os.path.join(model_path, 'model.safetensors')

        if not os.path.exists(model_path) or (not os.path.exists(bin_file) and not os.path.exists(safetensors_file)):
            raise IOError(f"在路径 '{model_path}' 中未找到任何有效的模型权重文件 ('pytorch_model.bin' 或 'model.safetensors')。")
        
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"PromiseDetector: 正在 {self.device} 上加载模型...")

        # from_pretrained 方法足够智能，会自动加载找到的权重文件，无需修改
        self.model = BertForSequenceClassification.from_pretrained(model_path).to(self.device)
        self.tokenizer = BertTokenizer.from_pretrained(model_path)
        
        self.model.eval()
        self.max_len = self.model.config.max_position_embeddings
        self.context_separator = ' [SEP] '
        
        print("PromiseDetector: 模型加载完成。")

    def predict(self, text, context=""):
        """
        对给定的文本（和可选的上下文）进行承诺检测。

        Args:
            text (str): 需要预测的当前句子。
            context (str, optional): 上下文句子。默认为空字符串。

        Returns:
            dict: 一个包含预测结果的字典。
        """
        if not isinstance(text, str) or not isinstance(context, str):
            raise TypeError("输入文本和上下文必须是字符串类型。")

        if context:
            combined_text = context + self.context_separator + text
        else:
            combined_text = text

        encoding = self.tokenizer.encode_plus(
            combined_text,
            add_special_tokens=True,
            max_length=self.max_len,
            return_token_type_ids=False,
            padding='max_length',
            truncation=True,
            return_attention_mask=True,
            return_tensors='pt',
        )

        input_ids = encoding['input_ids'].to(self.device)
        attention_mask = encoding['attention_mask'].to(self.device)

        with torch.no_grad():
            outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
        
        logits = outputs.logits
        probs = torch.softmax(logits, dim=1)
        prediction_idx = torch.argmax(probs, dim=1).item()
        confidence = probs[0][prediction_idx].item()
        
        return {
            "label": prediction_idx,
            "label_name": "Promise" if prediction_idx == 1 else "Not Promise",
            "confidence": confidence
        }